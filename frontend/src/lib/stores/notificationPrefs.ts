/**
 * Notification preferences: which events pop up, which sidebar badges show,
 * and which events go to Discord. Stored on the backend
 * (GET/PUT /api/notifications/preferences) so every browser and the desktop
 * app agree.
 *
 * The store starts at the catalog defaults and keeps them for any key the
 * backend does not return, so a slow or failed load never silences an alert
 * that is on by default. Locked safety alerts ignore the store entirely.
 */
import { get, writable } from 'svelte/store';
import { getNotificationPreferences, updateNotificationPreferences } from '$lib/api';
import {
	TEST_CATEGORY,
	defaultNotificationPrefs,
	getCategory,
	getNavBadge,
} from '$lib/notifications/catalog';

export type NotificationPrefs = Record<string, unknown>;

export const notificationPrefs = writable<NotificationPrefs>(defaultNotificationPrefs());

/** Pop-up on for this category? Locked safety alerts and tests always are. */
export function popupEnabled(prefs: NotificationPrefs, categoryId: string): boolean {
	if (categoryId === TEST_CATEGORY) return true;
	const category = getCategory(categoryId);
	if (!category?.popup) return false;
	if (category.popup.locked || !category.popup.key) return true;
	const value = prefs[category.popup.key];
	return typeof value === 'boolean' ? value : category.popup.default;
}

/** Is the sidebar badge for this route switched on? */
export function badgeEnabled(prefs: NotificationPrefs, href: string): boolean {
	const badge = getNavBadge(href);
	if (!badge) return false;
	const value = prefs[badge.key];
	return typeof value === 'boolean' ? value : badge.default;
}

let loadPromise: Promise<void> | null = null;

export function loadNotificationPrefs(): Promise<void> {
	if (!loadPromise) {
		loadPromise = getNotificationPreferences()
			.then((stored) => {
				if (stored && typeof stored === 'object') {
					notificationPrefs.set({ ...defaultNotificationPrefs(), ...stored });
				}
			})
			.catch(() => {
				// Keep the defaults; the next load (e.g. a later mount) retries.
			})
			.finally(() => {
				loadPromise = null;
			});
	}
	return loadPromise;
}

/**
 * Save some switches. Applied immediately (optimistic) and rolled back if the
 * backend refuses, so the caller can surface the error.
 */
export async function setNotificationPrefs(updates: Record<string, boolean>): Promise<void> {
	const keys = Object.keys(updates);
	const previous = get(notificationPrefs);
	notificationPrefs.update((current) => ({ ...current, ...updates }));
	try {
		const saved = (await updateNotificationPreferences(updates)) as NotificationPrefs | null;
		// Take the backend's (coerced) value for the keys this call wrote only:
		// another switch flipped meanwhile keeps its own in-flight value.
		if (saved && typeof saved === 'object') {
			notificationPrefs.update((current) => {
				const next = { ...current };
				for (const key of keys) if (key in saved) next[key] = saved[key];
				return next;
			});
		}
	} catch (error) {
		notificationPrefs.update((current) => {
			const next = { ...current };
			for (const key of keys) next[key] = previous[key];
			return next;
		});
		throw error;
	}
}

/** Every catalog switch back to its default (Discord transport untouched). */
export function resetNotificationPrefs(): Promise<void> {
	return setNotificationPrefs(defaultNotificationPrefs());
}
