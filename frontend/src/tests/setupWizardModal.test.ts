import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest';
import { mount, unmount } from 'svelte';

const { getForvenAuthProvidersMock, updateSettingsSectionMock } = vi.hoisted(() => ({
	getForvenAuthProvidersMock: vi.fn(),
	updateSettingsSectionMock: vi.fn(),
}));

vi.mock('$lib/api', () => ({
	getForvenAuthProviders: getForvenAuthProvidersMock,
	updateSettingsSection: updateSettingsSectionMock,
}));

import SetupWizardModal from '../lib/components/wizard/SetupWizardModal.svelte';
import { wizardOpen, wizardStep } from '../lib/stores/setupWizard';

let target: HTMLElement;
let instance: any;

afterEach(() => {
	if (instance) unmount(instance);
	target?.remove();
});

beforeEach(() => {
	getForvenAuthProvidersMock.mockReset();
	updateSettingsSectionMock.mockReset();
	getForvenAuthProvidersMock.mockResolvedValue([]);
	updateSettingsSectionMock.mockResolvedValue({});
	wizardOpen.set(true);
	wizardStep.set(0);
	if (typeof window !== 'undefined') {
		window.localStorage.clear();
	}
});

async function flush(): Promise<void> {
	await Promise.resolve();
	await Promise.resolve();
	await new Promise((r) => setTimeout(r, 0));
	await Promise.resolve();
}

describe('SetupWizardModal', () => {
	it('renders the welcome step text by default', async () => {
		target = document.createElement('div');
		document.body.appendChild(target);
		instance = mount(SetupWizardModal, {
			target,
			props: { settings: {} },
		});
		await flush();

		const text = target.textContent || '';
		expect(text).toContain('walks you through the minimum setup');
	});

	it('shows the critical banner on the trading step when unsatisfied', async () => {
		target = document.createElement('div');
		document.body.appendChild(target);
		instance = mount(SetupWizardModal, {
			target,
			props: { settings: {} },
		});
		wizardStep.set(1);
		await flush();

		const text = target.textContent || '';
		expect(text).toContain('Paper trading works without exchange keys');
		expect(text).toContain('Connect Hyperliquid to enable live orders');
		expect(text).not.toContain("can't place paper or live orders");
	});

	it('skip-all fires window.confirm when critical steps are unsatisfied', async () => {
		const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false);
		target = document.createElement('div');
		document.body.appendChild(target);
		instance = mount(SetupWizardModal, {
			target,
			props: { settings: {} },
		});
		await flush();

		const buttons = Array.from(target.querySelectorAll('button')) as HTMLButtonElement[];
		const skipBtn = buttons.find((b) => (b.textContent || '').includes('Skip all'));
		expect(skipBtn).toBeTruthy();
		skipBtn!.click();
		await flush();

		expect(confirmSpy).toHaveBeenCalled();
		expect(updateSettingsSectionMock).not.toHaveBeenCalled();
		confirmSpy.mockRestore();
	});
});
