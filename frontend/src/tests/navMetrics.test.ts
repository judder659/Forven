import { beforeEach, describe, expect, it } from 'vitest';
import { get } from 'svelte/store';
import { markNavSeen, navBadges, navIndicators, navSeenIds, setNavIndicators } from '../lib/stores/navMetrics';
import { notificationPrefs } from '../lib/stores/notificationPrefs';
import { defaultNotificationPrefs } from '../lib/notifications/catalog';
import type { SystemNavIndicator } from '../lib/api';

function count(ids: string[], overrides: Partial<SystemNavIndicator> = {}): SystemNavIndicator {
	return { kind: 'count', severity: 'warn', label: String(ids.length), summary: `${ids.length} waiting`, count: ids.length, item_ids: ids, ...overrides };
}

// The sidebar numbers are real counts. Visiting a page does not make a pending
// approval "go away" — deciding it does; visiting only dims the badge. Only
// Diagnostics counts news (issues raised since your last visit).
describe('sidebar badges', () => {
	beforeEach(() => {
		localStorage.clear();
		navSeenIds.set({});
		navIndicators.set({});
		notificationPrefs.set(defaultNotificationPrefs());
	});

	it('takes the first facts as the baseline: nothing that already existed is new', () => {
		setNavIndicators({ '/approval': count(['1', '2', '3']) });
		expect(get(navBadges)['/approval']).toMatchObject({ kind: 'count', count: 3, fresh: false });
	});

	it('keeps the real count after a visit and brightens only for new items', () => {
		setNavIndicators({ '/approval': count(['1', '2']) });
		markNavSeen('/approval');
		expect(get(navBadges)['/approval']).toMatchObject({ count: 2, fresh: false });

		setNavIndicators({ '/approval': count(['1', '2', '3']) });
		expect(get(navBadges)['/approval']).toMatchObject({ count: 3, fresh: true });

		markNavSeen('/approval');
		expect(get(navBadges)['/approval']).toMatchObject({ count: 3, fresh: false });

		// Deciding approvals is what clears the badge.
		setNavIndicators({ '/approval': count([]) });
		expect(get(navBadges)['/approval']).toBeNull();
	});

	it('counts only unseen issues on Diagnostics, and clears on a visit', () => {
		setNavIndicators({ '/diagnostics': count(['10', '9'], { severity: 'danger', danger_ids: ['10'] }) });
		expect(get(navBadges)['/diagnostics']).toBeNull();

		setNavIndicators({ '/diagnostics': count(['12', '11', '10', '9'], { severity: 'danger', danger_ids: ['10'] }) });
		expect(get(navBadges)['/diagnostics']).toMatchObject({ count: 2, severity: 'warn', fresh: true });

		setNavIndicators({ '/diagnostics': count(['13', '12', '11', '10', '9'], { severity: 'danger', danger_ids: ['13', '10'] }) });
		expect(get(navBadges)['/diagnostics']).toMatchObject({ count: 3, severity: 'danger' });

		markNavSeen('/diagnostics');
		expect(get(navBadges)['/diagnostics']).toBeNull();
	});

	it('does not re-light because an unrelated item changed', () => {
		setNavIndicators({ '/live-trades': count(['a', 'b'], { severity: 'info' }) });
		markNavSeen('/live-trades');
		// 'a' closed: fewer items, nothing new.
		setNavIndicators({ '/live-trades': count(['b'], { severity: 'info' }) });
		expect(get(navBadges)['/live-trades']).toMatchObject({ count: 1, fresh: false });
	});

	it('hides a badge the operator switched off, and keeps Paper Trades off by default', () => {
		setNavIndicators({ '/paper-trades': count(['p1'], { severity: 'info' }), '/bot-factory': count(['b1'], { severity: 'info' }) });
		expect(get(navBadges)['/paper-trades']).toBeNull();
		expect(get(navBadges)['/bot-factory']).toMatchObject({ count: 1 });

		notificationPrefs.set({ ...defaultNotificationPrefs(), badge_paper_trades: true, badge_bot_factory: false });
		expect(get(navBadges)['/paper-trades']).toMatchObject({ count: 1 });
		expect(get(navBadges)['/bot-factory']).toBeNull();
	});

	it('ignores routes the catalog has no badge for', () => {
		setNavIndicators({ '/risk': count(['x']), '/integrations': count(['y']) });
		expect(get(navIndicators)).toEqual({});
	});

	it('persists what was seen', () => {
		setNavIndicators({ '/approval': count(['1']) });
		setNavIndicators({ '/approval': count(['1', '2']) });
		markNavSeen('/approval');
		expect(JSON.parse(localStorage.getItem('forven.nav.seen_v2') ?? '{}')['/approval']).toEqual(['1', '2']);
	});
});
