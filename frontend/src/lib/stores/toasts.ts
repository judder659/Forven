/**
 * The bottom-right pop-up stack.
 *
 * Two ways in:
 *  - addToast(): feedback for something the operator just did ("Settings
 *    saved", "Start failed: …"). Always shown — muting notifications never
 *    hides the answer to your own click.
 *  - notify(): an event notification (a fill, an approval, a system alert).
 *    Shown only if its catalog category has pop-ups switched on
 *    (Settings → Notifications) and pop-ups are not snoozed. Critical safety
 *    alerts get through a snooze.
 *
 * Repeats of a visible notification fold into it ("×3") instead of stacking,
 * and the stack keeps at most MAX_VISIBLE_TOASTS: past that the oldest
 * non-critical pop-up makes room.
 */
import { get, writable } from 'svelte/store';
import { notificationPrefs, popupEnabled } from '$lib/stores/notificationPrefs';

export type ToastType = 'success' | 'error' | 'warning' | 'info';

export interface ToastItem {
	id: string;
	message: string;
	type: ToastType;
	href?: string;
	duration: number;
	/** Second line: strategy, reason, … */
	detail?: string;
	/** Notification catalog category (event notifications only). */
	category?: string;
	/** Gets past a snooze and is never pushed out of the stack. */
	critical?: boolean;
	/** Signed P&L fraction of a closed trade, drawn as a colored chip. */
	pnlPct?: number | null;
	/** How many identical notifications this pop-up stands for. */
	repeat: number;
	createdAt: number;
}

export interface NotifyInput {
	category: string;
	message: string;
	type?: ToastType;
	detail?: string;
	href?: string;
	pnlPct?: number | null;
	critical?: boolean;
	duration?: number;
}

export const MAX_VISIBLE_TOASTS = 5;
const DEFAULT_NOTIFY_MS = 6_000;
const CRITICAL_NOTIFY_MS = 15_000;
const SNOOZE_STORAGE_KEY = 'forven.notifications.snoozeUntil';

export const toasts = writable<ToastItem[]>([]);

function loadSnooze(): number {
	if (typeof window === 'undefined') return 0;
	try {
		const stored = Number(window.localStorage.getItem(SNOOZE_STORAGE_KEY) ?? 0);
		return Number.isFinite(stored) && stored > Date.now() ? stored : 0;
	} catch {
		return 0;
	}
}

/** Pop-ups are snoozed until this epoch-ms (0 = not snoozed). Survives reloads. */
export const snoozeUntil = writable<number>(loadSnooze());

snoozeUntil.subscribe((until) => {
	if (typeof window === 'undefined') return;
	try {
		if (until > Date.now()) window.localStorage.setItem(SNOOZE_STORAGE_KEY, String(until));
		else window.localStorage.removeItem(SNOOZE_STORAGE_KEY);
	} catch {
		// Storage blocked: the snooze still holds for this session.
	}
});

const SNOOZE_OPTIONS = [
	{ label: '15 min', ms: 15 * 60 * 1000 },
	{ label: '1 hour', ms: 60 * 60 * 1000 },
	{ label: '4 hours', ms: 4 * 60 * 60 * 1000 },
	{ label: '24 hours', ms: 24 * 60 * 60 * 1000 },
];

export function getSnoozeOptions() {
	return SNOOZE_OPTIONS;
}

export function isSnoozed(): boolean {
	return get(snoozeUntil) > Date.now();
}

export function snoozeNotifications(durationMs: number): void {
	snoozeUntil.set(Date.now() + durationMs);
	// Clear the event chatter already on screen; keep critical alerts and the
	// feedback for things the operator just did.
	toasts.update((list) => list.filter((toast) => toast.critical || !toast.category));
}

export function clearSnooze(): void {
	snoozeUntil.set(0);
}

let toastCounter = 0;

function pushToast(item: Omit<ToastItem, 'id' | 'createdAt' | 'repeat'>): string {
	const id = `toast-${++toastCounter}-${Date.now()}`;
	toasts.update((list) => {
		const next = [...list, { ...item, id, repeat: 1, createdAt: Date.now() }];
		// Over the cap: the oldest non-critical pop-up makes room.
		while (next.length > MAX_VISIBLE_TOASTS) {
			const index = next.findIndex((toast) => !toast.critical);
			if (index < 0) break;
			next.splice(index, 1);
		}
		return next;
	});
	return id;
}

/** Feedback for an operator action. Always shown. */
export function addToast(
	message: string,
	type: ToastType = 'info',
	href?: string,
	duration = 5000,
): string | null {
	return pushToast({ message, type, href, duration });
}

/** An event notification, subject to the operator's pop-up switches and snooze. */
export function notify(input: NotifyInput): string | null {
	if (!popupEnabled(get(notificationPrefs), input.category)) return null;
	const critical = Boolean(input.critical);
	if (!critical && isSnoozed()) return null;

	// A repeat of a pop-up still on screen folds into it and restarts its timer.
	const existing = get(toasts).find(
		(toast) => toast.category === input.category && toast.message === input.message && toast.detail === input.detail,
	);
	if (existing) {
		toasts.update((list) =>
			list.map((toast) =>
				toast.id === existing.id ? { ...toast, repeat: toast.repeat + 1, createdAt: Date.now() } : toast,
			),
		);
		return existing.id;
	}

	return pushToast({
		message: input.message,
		type: input.type ?? 'info',
		href: input.href,
		detail: input.detail,
		category: input.category,
		critical,
		pnlPct: input.pnlPct,
		duration: input.duration ?? (critical ? CRITICAL_NOTIFY_MS : DEFAULT_NOTIFY_MS),
	});
}

export function dismissToast(id: string): void {
	toasts.update((list) => list.filter((toast) => toast.id !== id));
}
