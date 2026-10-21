import { describe, it, expect, afterEach, vi } from 'vitest';
import { mount, unmount } from 'svelte';
import SettingsSidebar from '../lib/components/settings/shell/SettingsSidebar.svelte';
import { SETTINGS_AREAS } from '../lib/settings/manifest';

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

describe('SettingsSidebar', () => {
	it('renders manifest areas in order, Home first and Danger last', async () => {
		target = document.createElement('div');
		document.body.appendChild(target);
		instance = mount(SettingsSidebar, {
			target,
			props: {
				active: 'home',
				onChange: vi.fn(),
			},
		});
		await flush();

		const buttons = target.querySelectorAll('button');
		expect(buttons.length).toBe(SETTINGS_AREAS.length);
		expect(buttons[0].textContent).toContain('Overview');
		expect(buttons[buttons.length - 1].textContent).toContain('Danger');
	});

	it('fires onChange with area id on click', async () => {
		const onChange = vi.fn();
		target = document.createElement('div');
		document.body.appendChild(target);
		instance = mount(SettingsSidebar, {
			target,
			props: {
				active: 'home',
				onChange,
			},
		});
		await flush();

		const buttons = target.querySelectorAll('button');
		let tradingBtn: HTMLButtonElement | null = null;
		buttons.forEach((btn) => {
			if (btn.textContent && btn.textContent.includes('Live trading')) {
				tradingBtn = btn as HTMLButtonElement;
			}
		});
		expect(tradingBtn).toBeTruthy();

		tradingBtn!.click();
		await flush();

		expect(onChange).toHaveBeenCalledTimes(1);
		expect(onChange).toHaveBeenCalledWith('trading');
	});

	it('marks the active area button with aria-current="page"', async () => {
		target = document.createElement('div');
		document.body.appendChild(target);
		instance = mount(SettingsSidebar, {
			target,
			props: {
				active: 'trading',
				onChange: vi.fn(),
			},
		});
		await flush();

		const buttons = target.querySelectorAll('button');
		let tradingBtn: HTMLButtonElement | null = null;
		let homeBtn: HTMLButtonElement | null = null;
		buttons.forEach((btn) => {
			if (btn.textContent && btn.textContent.includes('Live trading')) {
				tradingBtn = btn as HTMLButtonElement;
			}
			if (btn.textContent && btn.textContent.includes('Overview')) {
				homeBtn = btn as HTMLButtonElement;
			}
		});
		expect(tradingBtn).toBeTruthy();
		expect(homeBtn).toBeTruthy();

		expect(tradingBtn!.getAttribute('aria-current')).toBe('page');
		expect(homeBtn!.getAttribute('aria-current')).toBeNull();
	});

	it('applies a red text color to the Danger Zone button when inactive', async () => {
		target = document.createElement('div');
		document.body.appendChild(target);
		instance = mount(SettingsSidebar, {
			target,
			props: {
				active: 'home',
				onChange: vi.fn(),
			},
		});
		await flush();

		const buttons = target.querySelectorAll('button');
		let dangerBtn: HTMLButtonElement | null = null;
		buttons.forEach((btn) => {
			if (btn.textContent && btn.textContent.includes('Danger')) {
				dangerBtn = btn as HTMLButtonElement;
			}
		});
		expect(dangerBtn).toBeTruthy();
		expect(dangerBtn!.className).toContain('text-red-400');
	});

	it('does not crash when onChange is omitted and a button is clicked', async () => {
		target = document.createElement('div');
		document.body.appendChild(target);
		instance = mount(SettingsSidebar, {
			target,
			props: {
				active: 'home',
			} as any,
		});
		await flush();

		const buttons = target.querySelectorAll('button');
		let tradingBtn: HTMLButtonElement | null = null;
		buttons.forEach((btn) => {
			if (btn.textContent && btn.textContent.includes('Live trading')) {
				tradingBtn = btn as HTMLButtonElement;
			}
		});
		expect(tradingBtn).toBeTruthy();

		tradingBtn!.click();
		await flush();

		expect(target.isConnected).toBe(true);
	});
});
