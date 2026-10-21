import { describe, it, expect, afterEach } from 'vitest';
import { mount, unmount } from 'svelte';
import SettingsSubsection from '../lib/components/settings/primitives/SettingsSubsection.svelte';

let target: HTMLElement;
let instance: any;

afterEach(() => {
	if (instance) unmount(instance);
	target?.remove();
});

async function flush(): Promise<void> {
	await Promise.resolve();
	await Promise.resolve();
	await new Promise((r) => setTimeout(r, 0));
	await Promise.resolve();
}

describe('SettingsSubsection', () => {
	it('renders label, description, and deep-link anchor', async () => {
		target = document.createElement('div');
		document.body.appendChild(target);
		instance = mount(SettingsSubsection, {
			target,
			props: {
				label: 'Risk Management',
				description: 'Controls when new trades are blocked.',
				deepLinkTo: '/risk-monitor',
			},
		});
		await flush();

		expect(target.textContent).toContain('Risk Management');
		expect(target.textContent).toContain('Controls when new trades are blocked.');

		const anchor = target.querySelector('a[href="/risk-monitor"]') as HTMLAnchorElement | null;
		expect(anchor).toBeTruthy();
		expect(anchor!.textContent).toContain('/risk-monitor');
	});

	it('folds a collapsed group until its toggle is clicked', async () => {
		target = document.createElement('div');
		document.body.appendChild(target);
		instance = mount(SettingsSubsection, {
			target,
			props: { label: 'X', description: 'Folded body.', collapsed: true },
		});
		await flush();

		const body = target.querySelector('section > div') as HTMLElement;
		expect(body.hidden).toBe(true);
		const toggle = target.querySelector('button[aria-expanded]') as HTMLButtonElement | null;
		expect(toggle).toBeTruthy();
		expect(toggle!.getAttribute('aria-expanded')).toBe('false');

		toggle!.click();
		await flush();

		expect(toggle!.getAttribute('aria-expanded')).toBe('true');
		expect(body.hidden).toBe(false);
	});

	it('shows the changed count and resets only after a second click', async () => {
		let resets = 0;
		target = document.createElement('div');
		document.body.appendChild(target);
		instance = mount(SettingsSubsection, {
			target,
			props: { label: 'X', description: '.', changedCount: 2, onReset: () => resets++ },
		});
		await flush();

		expect(target.querySelector('[data-testid="changed-count"]')!.textContent).toContain('2 changed');
		const reset = target.querySelector('[data-testid="reset-group"]') as HTMLButtonElement;
		reset.click();
		await flush();
		expect(resets).toBe(0);
		expect(reset.textContent).toContain('Click again');
		reset.click();
		await flush();
		expect(resets).toBe(1);
	});

	it('omits the deep-link anchor when deepLinkTo is undefined', async () => {
		target = document.createElement('div');
		document.body.appendChild(target);
		instance = mount(SettingsSubsection, {
			target,
			props: {
				label: 'Plain section',
				description: 'No deep link here.',
			},
		});
		await flush();

		expect(target.querySelector('a')).toBeNull();
	});
});
