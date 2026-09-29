/**
 * The notification catalog: every kind of event Forven tells you about, the
 * channels it can reach (pop-up, Discord) and the switches for them, plus the
 * sidebar badges.
 *
 * The backend owns it (forven/notification_catalog.py); this module types the
 * checked-in snapshot, regenerated with `python -m forven.notification_catalog`
 * and pinned fresh by tests/test_notification_catalog.py.
 */
import CATALOG from './catalog.generated.json';

export interface NotificationChannelSwitch {
	/** Preference key for the switch; null when the channel is locked on. */
	key: string | null;
	default: boolean;
	locked: boolean;
}

export interface NotificationDiscordSwitch {
	/** Preference keys the Discord switch sets together (empty when locked). */
	keys: string[];
	default: boolean;
	locked: boolean;
	note: string;
}

export interface NotificationCategory {
	id: string;
	group: string;
	label: string;
	description: string;
	/** null: this event never pops up. */
	popup: NotificationChannelSwitch | null;
	/** null: this event never goes to Discord. */
	discord: NotificationDiscordSwitch | null;
}

export interface NotificationGroup {
	id: string;
	label: string;
}

/** 'total': how many items are open now. 'unread': only ones you have not seen. */
export type NavBadgeMode = 'total' | 'unread';

export interface NavBadgeDefinition {
	id: string;
	href: string;
	label: string;
	description: string;
	key: string;
	default: boolean;
	mode: NavBadgeMode;
}

/** The pop-up category behind "Send a test notification": always shown. */
export const TEST_CATEGORY = 'test';

export const NOTIFICATION_GROUPS: NotificationGroup[] = CATALOG.groups;
export const NOTIFICATION_CATEGORIES: NotificationCategory[] = CATALOG.categories as NotificationCategory[];
export const NAV_BADGES: NavBadgeDefinition[] = CATALOG.badges as NavBadgeDefinition[];

const CATEGORY_BY_ID = new Map(NOTIFICATION_CATEGORIES.map((category) => [category.id, category]));
const BADGE_BY_HREF = new Map(NAV_BADGES.map((badge) => [badge.href, badge]));

export function getCategory(id: string | null | undefined): NotificationCategory | undefined {
	return id ? CATEGORY_BY_ID.get(id) : undefined;
}

export function getNavBadge(href: string): NavBadgeDefinition | undefined {
	return BADGE_BY_HREF.get(href);
}

/** Every switch the catalog names, at its default. */
export function defaultNotificationPrefs(): Record<string, boolean> {
	const defaults: Record<string, boolean> = {};
	for (const category of NOTIFICATION_CATEGORIES) {
		if (category.popup?.key) defaults[category.popup.key] = category.popup.default;
		for (const key of category.discord?.keys ?? []) {
			if (!(key in defaults)) defaults[key] = category.discord?.default ?? true;
		}
	}
	for (const badge of NAV_BADGES) defaults[badge.key] = badge.default;
	return defaults;
}
