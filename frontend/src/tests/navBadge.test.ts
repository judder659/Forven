import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { mount, unmount } from 'svelte';
import NavBadge from '../lib/components/NavBadge.svelte';
import type { NavBadgeView } from '../lib/stores/navMetrics';

type MountedComponent = ReturnType<typeof mount>;

function view(overrides: Partial<NavBadgeView>): NavBadgeView {
	return {
		kind: 'count',
		count: 0,
		label: '',
		severity: 'info',
		summary: '',
		fresh: false,
		...overrides,
	};
}

// The badge draws what navMetrics.navBadges decided: the number is the real
// count (it does not vanish on a visit — resolving the items clears it), and
// `fresh` only changes how loud it looks.
describe('NavBadge', () => {
	let target: HTMLDivElement;
	let app: MountedComponent | null = null;

	beforeEach(() => {
		target = document.createElement('div');
		document.body.appendChild(target);
	});

	afterEach(() => {
		if (app) {
			unmount(app);
			app = null;
		}
		target.remove();
	});

	it('shows the count', () => {
		app = mount(NavBadge, { target, props: { badge: view({ count: 9, summary: '9 live positions open' }) } });
		expect(target.textContent?.trim()).toBe('9');
		expect(target.querySelector('[title]')?.getAttribute('title')).toContain('9 live positions open');
	});

	it('draws a fresh badge bright and a seen one dimmed, with the same number', () => {
		app = mount(NavBadge, { target, props: { badge: view({ count: 3, severity: 'warn', fresh: true }) } });
		const fresh = target.querySelector('span');
		expect(fresh?.textContent?.trim()).toBe('3');
		expect(fresh?.getAttribute('data-fresh')).toBe('true');
		expect(fresh?.className).toContain('bg-yellow-500/15');
		unmount(app);

		app = mount(NavBadge, { target, props: { badge: view({ count: 3, severity: 'warn', fresh: false }) } });
		const seen = target.querySelector('span');
		expect(seen?.textContent?.trim()).toBe('3');
		expect(seen?.getAttribute('data-fresh')).toBe('false');
		expect(seen?.className).not.toContain('bg-yellow-500/15');
	});

	it('caps the number at 99+', () => {
		app = mount(NavBadge, { target, props: { badge: view({ count: 250 }) } });
		expect(target.textContent?.trim()).toBe('99+');
	});

	it('keeps a status pill bright even once seen (standing hazard)', () => {
		app = mount(NavBadge, {
			target,
			props: { badge: view({ kind: 'status', label: 'STALE', severity: 'danger', fresh: false, summary: '1 live series is late' }) },
		});
		expect(target.textContent).toContain('STALE');
		expect(target.querySelector('span')?.className).toContain('bg-red-500/15');
	});

	it('renders nothing without a badge or with a zero count', () => {
		app = mount(NavBadge, { target, props: { badge: null } });
		expect(target.textContent?.trim()).toBe('');
		unmount(app);
		app = mount(NavBadge, { target, props: { badge: view({ count: 0 }) } });
		expect(target.textContent?.trim()).toBe('');
	});
});
