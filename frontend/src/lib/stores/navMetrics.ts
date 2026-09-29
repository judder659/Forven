/**
 * Sidebar badges.
 *
 * The facts come from the heartbeat: per page, how many items and which ones
 * (nav_indicators; the /data badge is built client-side from the SLA census).
 * Which pages carry a badge, and how it counts, comes from the notification
 * catalog; whether it shows at all is the operator's switch in
 * Settings → Notifications.
 *
 * Two kinds of badge:
 *  - 'total' (Approvals, Live Trades, Data, Bot Factory, Paper Trades): the real
 *    number of open items, shown until they are resolved. It is bright while it
 *    holds an item you have not seen on that page yet, and dims once you have.
 *  - 'unread' (Diagnostics): only issues raised since you last opened the page.
 *
 * "Seen" is kept per item id (localStorage), so a badge never re-lights because
 * an unrelated item changed, and never stays quiet about one you have not seen.
 * The first time a page's facts arrive they become the baseline: nothing that
 * already existed is announced as new.
 */
import { derived, get, writable } from 'svelte/store';
import type { NavIndicatorSeverity, SystemNavIndicator } from '$lib/api';
import { NAV_BADGES, getNavBadge } from '$lib/notifications/catalog';
import { badgeEnabled, notificationPrefs } from '$lib/stores/notificationPrefs';

const SEEN_STORAGE_KEY = 'forven.nav.seen_v2';
const LEGACY_SEEN_STORAGE_KEY = 'forven.nav.seen_v1';

export interface NavBadgeView {
	kind: 'count' | 'status';
	/** The number to show (unseen items only, for an 'unread' badge). */
	count: number;
	/** Status pill text (e.g. STALE). */
	label: string;
	severity: NavIndicatorSeverity;
	summary: string;
	/** Holds items not seen yet: drawn bright rather than dimmed. */
	fresh: boolean;
}

type SeenIds = Record<string, string[]>;

function loadSeen(): SeenIds {
	if (typeof window === 'undefined') return {};
	try {
		window.localStorage.removeItem(LEGACY_SEEN_STORAGE_KEY);
		const stored = window.localStorage.getItem(SEEN_STORAGE_KEY);
		const parsed = stored ? JSON.parse(stored) : {};
		return parsed && typeof parsed === 'object' ? (parsed as SeenIds) : {};
	} catch {
		return {};
	}
}

function saveSeen(seen: SeenIds): void {
	if (typeof window === 'undefined') return;
	try {
		window.localStorage.setItem(SEEN_STORAGE_KEY, JSON.stringify(seen));
	} catch {
		// Storage full or blocked: seen state still holds for this session.
	}
}

function idsOf(indicator: SystemNavIndicator | undefined): string[] {
	return Array.isArray(indicator?.item_ids) ? indicator.item_ids.map(String) : [];
}

/** The latest facts per page (only pages the catalog has a badge for). */
export const navIndicators = writable<Record<string, SystemNavIndicator>>({});

/** Item ids already seen per page. */
export const navSeenIds = writable<SeenIds>(loadSeen());

export function setNavIndicators(indicators: Record<string, SystemNavIndicator> | undefined): void {
	const next: Record<string, SystemNavIndicator> = {};
	if (indicators && typeof indicators === 'object') {
		for (const badge of NAV_BADGES) {
			const indicator = indicators[badge.href];
			if (indicator && typeof indicator === 'object') next[badge.href] = indicator;
		}
	}

	// First sight of a page's facts is the baseline, not news.
	const seen = get(navSeenIds);
	const missing = Object.keys(next).filter((href) => !Array.isArray(seen[href]));
	if (missing.length > 0) {
		const baselined = { ...seen };
		for (const href of missing) baselined[href] = idsOf(next[href]);
		navSeenIds.set(baselined);
		saveSeen(baselined);
	}

	navIndicators.set(next);
}

/** The operator is looking at this page: everything on it now counts as seen. */
export function markNavSeen(href: string): void {
	if (!getNavBadge(href)) return;
	const indicator = get(navIndicators)[href];
	if (!indicator) return;
	const ids = idsOf(indicator);
	const seen = get(navSeenIds);
	const current = seen[href];
	// No-op when nothing changed: the sidebar calls this reactively on every
	// refresh while the page is open, and an unconditional set would loop.
	if (Array.isArray(current) && current.length === ids.length && ids.every((id) => current.includes(id))) return;
	const next = { ...seen, [href]: ids };
	navSeenIds.set(next);
	saveSeen(next);
}

function toView(
	mode: 'total' | 'unread',
	indicator: SystemNavIndicator | undefined,
	seenIds: string[] | undefined,
): NavBadgeView | null {
	if (!indicator || indicator.kind === 'none') return null;
	const seen = new Set(seenIds ?? []);
	const unseen = idsOf(indicator).filter((id) => !seen.has(id));
	const summary = String(indicator.summary ?? '');

	if (mode === 'unread') {
		if (unseen.length === 0) return null;
		const danger = new Set((indicator.danger_ids ?? []).map(String));
		return {
			kind: 'count',
			count: unseen.length,
			label: '',
			severity: unseen.some((id) => danger.has(id)) ? 'danger' : 'warn',
			summary: `${unseen.length} new ${unseen.length === 1 ? 'issue' : 'issues'} since you last looked`,
			fresh: true,
		};
	}

	const count = Math.max(0, Number(indicator.count ?? 0) || 0);
	const fresh = unseen.length > 0;
	const tooltip = fresh ? `${summary} · new since you last looked` : summary;
	if (indicator.kind === 'status') {
		return { kind: 'status', count, label: String(indicator.label ?? ''), severity: indicator.severity, summary: tooltip, fresh };
	}
	if (count <= 0) return null;
	return { kind: 'count', count, label: '', severity: indicator.severity, summary: tooltip, fresh };
}

/** What each sidebar link shows, or null for no badge. */
export const navBadges = derived(
	[navIndicators, navSeenIds, notificationPrefs],
	([$indicators, $seen, $prefs]) => {
		const views: Record<string, NavBadgeView | null> = {};
		for (const badge of NAV_BADGES) {
			views[badge.href] = badgeEnabled($prefs, badge.href)
				? toView(badge.mode, $indicators[badge.href], $seen[badge.href])
				: null;
		}
		return views;
	},
);
