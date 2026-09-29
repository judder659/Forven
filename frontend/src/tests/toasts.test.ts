import { beforeEach, describe, expect, it } from 'vitest';
import { get } from 'svelte/store';
import {
	MAX_VISIBLE_TOASTS,
	addToast,
	clearSnooze,
	notify,
	snoozeNotifications,
	snoozeUntil,
	toasts,
} from '../lib/stores/toasts';
import { notificationPrefs } from '../lib/stores/notificationPrefs';
import { defaultNotificationPrefs } from '../lib/notifications/catalog';

describe('pop-up stack', () => {
	beforeEach(() => {
		localStorage.clear();
		toasts.set([]);
		snoozeUntil.set(0);
		notificationPrefs.set(defaultNotificationPrefs());
	});

	it('shows an event only when its category pops up', () => {
		expect(notify({ category: 'live_trade_opened', message: 'Live trade opened: LONG BTC' })).not.toBeNull();
		expect(notify({ category: 'paper_trade_opened', message: 'Paper trade opened: LONG BTC' })).toBeNull();
		expect(notify({ category: 'agent_completion', message: 'no pop-up exists for this' })).toBeNull();
		expect(notify({ category: 'not_in_the_catalog', message: 'unknown' })).toBeNull();
		expect(get(toasts).map((toast) => toast.category)).toEqual(['live_trade_opened']);
	});

	it('falls back to the catalog default for a switch the backend has not sent', () => {
		notificationPrefs.set({});
		expect(notify({ category: 'approval_required', message: 'Approval needed' })).not.toBeNull();
		expect(notify({ category: 'paper_trade_closed', message: 'quiet by default' })).toBeNull();
	});

	it('snooze drops events but never critical alerts or action feedback', () => {
		snoozeNotifications(60_000);
		expect(notify({ category: 'live_trade_opened', message: 'routine' })).toBeNull();
		expect(notify({ category: 'risk_halt', message: 'Kill switch activated', critical: true })).not.toBeNull();
		expect(addToast('Settings saved', 'success')).not.toBeNull();

		expect(get(toasts).map((toast) => toast.message)).toEqual(['Kill switch activated', 'Settings saved']);
	});

	it('clears the event chatter on screen when snoozing, keeping critical alerts and feedback', () => {
		notify({ category: 'live_trade_opened', message: 'routine' });
		notify({ category: 'risk_halt', message: 'Kill switch activated', critical: true });
		addToast('Start failed: boom', 'error');

		snoozeNotifications(60_000);

		expect(get(toasts).map((toast) => toast.message)).toEqual(['Kill switch activated', 'Start failed: boom']);
	});

	it('remembers a snooze across reloads', () => {
		const until = Date.now() + 60_000;
		snoozeUntil.set(until);
		expect(localStorage.getItem('forven.notifications.snoozeUntil')).toBe(String(until));
		clearSnooze();
		expect(localStorage.getItem('forven.notifications.snoozeUntil')).toBeNull();
	});

	it('folds a repeat into the pop-up already on screen', () => {
		const first = notify({ category: 'live_entry_blocked', message: 'Live open blocked (BTC)', detail: 'S1: cap' });
		const second = notify({ category: 'live_entry_blocked', message: 'Live open blocked (BTC)', detail: 'S1: cap' });

		const list = get(toasts);
		expect(second).toBe(first);
		expect(list).toHaveLength(1);
		expect(list[0].repeat).toBe(2);
	});

	it('keeps at most MAX_VISIBLE_TOASTS, dropping the oldest non-critical first', () => {
		notify({ category: 'risk_halt', message: 'critical one', critical: true });
		for (let i = 0; i < MAX_VISIBLE_TOASTS + 2; i += 1) {
			notify({ category: 'live_trade_opened', message: `fill ${i}` });
		}

		const messages = get(toasts).map((toast) => toast.message);
		expect(messages).toHaveLength(MAX_VISIBLE_TOASTS);
		expect(messages[0]).toBe('critical one');
		expect(messages.at(-1)).toBe(`fill ${MAX_VISIBLE_TOASTS + 1}`);
	});
});
