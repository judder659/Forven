import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { get } from 'svelte/store';
import { startNotificationRouter, stopNotificationRouter } from '../lib/stores/notificationRouter';
import { snoozeUntil, toasts } from '../lib/stores/toasts';
import { notificationPrefs } from '../lib/stores/notificationPrefs';
import { defaultNotificationPrefs } from '../lib/notifications/catalog';

function emit(detail: Record<string, unknown>): void {
	window.dispatchEvent(new CustomEvent('forven:event', { detail }));
}

function allowPopups(...keys: string[]): void {
	notificationPrefs.set({ ...defaultNotificationPrefs(), ...Object.fromEntries(keys.map((key) => [key, true])) });
}

// Realtime events → pop-ups, gated by the operator's switches (Settings →
// Notifications). The sidebar badges are real counts from the heartbeat, so
// the router never touches them.
describe('notificationRouter', () => {
	beforeEach(() => {
		localStorage.clear();
		toasts.set([]);
		snoozeUntil.set(0);
		notificationPrefs.set(defaultNotificationPrefs());
		startNotificationRouter();
	});

	afterEach(() => {
		stopNotificationRouter();
	});

	it('pops up live fills and routes them to Live Trades', () => {
		emit({
			type: 'trade',
			data: { opened: [{ id: 'l1', asset: 'BTC', direction: 'long', strategy: 'Momentum', execution_type: 'live', source: 'scanner' }], closed: [] },
		});

		const list = get(toasts);
		expect(list).toHaveLength(1);
		expect(list[0]).toMatchObject({ category: 'live_trade_opened', type: 'success', href: '/live-trades', detail: 'Momentum' });
		expect(list[0].message).toContain('LONG BTC');
	});

	it('keeps paper fills quiet by default — the operator asked for that', () => {
		emit({
			type: 'trade',
			data: {
				opened: [{ id: 'p1', asset: 'BTC', direction: 'long', execution_type: 'paper', source: 'scanner' }],
				closed: [{ id: 'p0', asset: 'ETH', direction: 'short', execution_type: 'paper', pnl_pct: -0.01 }],
			},
		});

		expect(get(toasts)).toHaveLength(0);
	});

	it('pops up paper fills once their switches are on', () => {
		allowPopups('popup_paper_trade_opened', 'popup_paper_trade_closed');
		emit({
			type: 'trade',
			data: {
				opened: [{ id: 'p1', asset: 'BTC', direction: 'long', execution_type: 'paper' }],
				closed: [{ id: 'p0', asset: 'ETH', direction: 'short', execution_type: 'paper', pnl_pct: 0.02 }],
			},
		});

		const list = get(toasts);
		expect(list.map((toast) => toast.category)).toEqual(['paper_trade_opened', 'paper_trade_closed']);
		expect(list.every((toast) => toast.href === '/paper-trades')).toBe(true);
	});

	it('shows a losing close as information with a red P&L chip, not as an error', () => {
		emit({
			type: 'trade',
			data: { opened: [], closed: [{ id: 'l1', asset: 'ETH', direction: 'short', strategy: 'MeanRev', execution_type: 'live', pnl_pct: -0.0123 }] },
		});

		const [toast] = get(toasts);
		// An 'error' type used to get past a snooze: a losing trade is not an error.
		expect(toast.type).toBe('info');
		expect(toast.pnlPct).toBe(-0.0123);
		expect(toast.critical).toBe(false);
	});

	it('collapses a mass close into one pop-up per kind', () => {
		const closed = Array.from({ length: 5 }, (_, i) => ({ id: `t${i}`, asset: 'BTC', direction: 'long', execution_type: 'live', pnl_pct: 0.01 }));
		emit({ type: 'trade', data: { opened: [], closed } });

		const list = get(toasts);
		expect(list).toHaveLength(1);
		expect(list[0].message).toBe('Live: closed 5 positions');
		expect(list[0].pnlPct).toBeCloseTo(0.01);
		expect(list[0].href).toBe('/live-trades');
	});

	it('routes bot trades to the Bot Factory', () => {
		emit({ type: 'trade', data: { opened: [{ id: 'b1', asset: 'DOGE', direction: 'long', source: 'bot:42' }], closed: [] } });

		const [toast] = get(toasts);
		expect(toast).toMatchObject({ category: 'bot_trades', href: '/bot-factory' });
	});

	it('ignores activity-log shaped trade events so a fill never notifies twice', () => {
		emit({ type: 'trade', data: { level: 'trade', message: 'Opened long BTC' } });
		expect(get(toasts)).toHaveLength(0);
	});

	it('pops up an approval with its reason', () => {
		emit({
			type: 'approval_created',
			data: { id: 7, approval_type: 'strategy_promotion_approval', reason: 'Promote S0123 to paper' },
		});

		const [toast] = get(toasts);
		expect(toast).toMatchObject({ category: 'approval_required', type: 'warning', href: '/approval', detail: 'Promote S0123 to paper' });
	});

	it('pops up kill-switch flips only from state-diff payloads, even when snoozed', () => {
		snoozeUntil.set(Date.now() + 60_000);
		emit({ type: 'kill_switch_activated', data: { kill_switch_active: true, ts: 'x' } });
		// Activity-log classified copy of the same fact carries no boolean.
		emit({ type: 'kill_switch_activated', data: { message: 'Kill switch engaged (log row)' } });

		const list = get(toasts);
		expect(list).toHaveLength(1);
		expect(list[0]).toMatchObject({ category: 'risk_halt', type: 'error', critical: true });
	});

	it('cannot silence a locked safety alert', () => {
		notificationPrefs.set({ ...defaultNotificationPrefs(), popup_risk_halt: false, popup_live_order_failure: false });
		emit({ type: 'risk_alert', data: { kind: 'daily_loss_halt', daily_loss_halt: true } });
		emit({
			type: 'notification',
			data: { id: 5, event_type: 'trade_failed', category: 'live_order_failure', severity: 'warn', title: 'Trade execution failed (open)' },
		});

		expect(get(toasts).map((toast) => toast.category)).toEqual(['risk_halt', 'live_order_failure']);
	});

	it('pops up backend notifications by their category switch', () => {
		emit({
			type: 'notification',
			data: { id: 1, event_type: 'health_critical', category: 'system_critical', severity: 'critical', title: 'CRITICAL: scanner_execution', summary: 'Scanner stalled' },
		});
		// System warnings are off by default.
		emit({ type: 'notification', data: { id: 2, event_type: 'system_degraded', category: 'system_warning', severity: 'warn', title: 'Queue slow' } });
		// Info with no category (agent task finished) never pops up.
		emit({ type: 'notification', data: { id: 3, event_type: 'agent_task_completed', category: 'agent_completion', severity: 'info', title: 'done' } });

		const list = get(toasts);
		expect(list).toHaveLength(1);
		expect(list[0]).toMatchObject({ category: 'system_critical', type: 'error', message: 'CRITICAL: scanner_execution', detail: 'Scanner stalled', critical: true });
	});

	it('never pops up a backend row for a fact the realtime diffs already cover', () => {
		allowPopups('popup_paper_trade_opened');
		emit({ type: 'notification', data: { id: 9, event_type: 'trade_opened', category: 'live_trade_opened', severity: 'info', title: 'LIVE signal S1' } });
		emit({ type: 'notification', data: { id: 10, event_type: 'approval_required', category: 'approval_required', severity: 'info', title: 'Approve?' } });

		expect(get(toasts)).toHaveLength(0);
	});

	it('always shows the Settings test notification', () => {
		notificationPrefs.set({});
		emit({ type: 'notification', data: { id: 11, event_type: 'notification_test', category: 'test', severity: 'info', title: 'Test notification' } });

		expect(get(toasts)[0]).toMatchObject({ category: 'test', type: 'success', href: '/settings#notifications' });
	});

	it('names the AI client that connected, once its pop-up is on', () => {
		emit({ type: 'mcp_session_opened', data: { message: 'AI Drop Zone session s1 opened by claude-code', data: '{"actor": "claude-code"}' } });
		expect(get(toasts)).toHaveLength(0);

		allowPopups('popup_mcp_session');
		emit({ type: 'mcp_session_opened', data: { message: 'AI Drop Zone session s2 opened by codex', data: '{"actor": "codex"}' } });
		expect(get(toasts)[0]).toMatchObject({ category: 'mcp_session', detail: 'codex', href: '/integrations' });
	});
});
