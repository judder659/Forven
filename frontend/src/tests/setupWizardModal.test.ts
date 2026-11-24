import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest';
import { mount, unmount } from 'svelte';

const {
	getForvenAuthProvidersMock,
	reconcileAgentProvidersMock,
	testForvenAuthProviderMock,
	updateSettingsSectionMock,
	listWalletsMock,
	getCatalogMock,
	listJobsMock,
	startDownloadsMock,
	gotoMock,
} = vi.hoisted(() => ({
	getForvenAuthProvidersMock: vi.fn(),
	reconcileAgentProvidersMock: vi.fn(),
	testForvenAuthProviderMock: vi.fn(),
	updateSettingsSectionMock: vi.fn(),
	listWalletsMock: vi.fn(),
	getCatalogMock: vi.fn(),
	listJobsMock: vi.fn(),
	startDownloadsMock: vi.fn(),
	gotoMock: vi.fn(),
}));

vi.mock('$app/navigation', () => ({
	goto: gotoMock,
}));

vi.mock('$lib/api', () => ({
	getForvenAuthProviders: getForvenAuthProvidersMock,
	reconcileAgentProviders: reconcileAgentProvidersMock,
	testForvenAuthProvider: testForvenAuthProviderMock,
	updateSettingsSection: updateSettingsSectionMock,
}));

vi.mock('$lib/api/wallets', () => ({
	listWallets: listWalletsMock,
}));

vi.mock('$lib/api/dataManager', () => ({
	getCatalog: getCatalogMock,
	listJobs: listJobsMock,
	startDownloads: startDownloadsMock,
}));

import SetupWizardModal from '../lib/components/wizard/SetupWizardModal.svelte';
import { wizardOpen, wizardStep } from '../lib/stores/setupWizard';

let target: HTMLElement;
let instance: ReturnType<typeof mount> | null = null;

afterEach(() => {
	if (instance) {
		unmount(instance);
		instance = null;
	}
	target?.remove();
});

beforeEach(() => {
	getForvenAuthProvidersMock.mockReset();
	reconcileAgentProvidersMock.mockReset();
	testForvenAuthProviderMock.mockReset();
	updateSettingsSectionMock.mockReset();
	listWalletsMock.mockReset();
	getCatalogMock.mockReset();
	listJobsMock.mockReset();
	startDownloadsMock.mockReset();
	gotoMock.mockReset();

	getForvenAuthProvidersMock.mockResolvedValue({
		providers: [
			{ provider: 'openai', status: 'not_configured', configured: false, supports_oauth: true, requires_token: true },
			{ provider: 'anthropic', status: 'not_configured', configured: false, supports_oauth: false, requires_token: true },
			{ provider: 'gemini', status: 'not_configured', configured: false, supports_oauth: false, requires_token: true },
			{ provider: 'deepseek', status: 'not_configured', configured: false, supports_oauth: false, requires_token: true },
		],
	});
	reconcileAgentProvidersMock.mockResolvedValue({ updated: 1, provider: 'openai' });
	testForvenAuthProviderMock.mockResolvedValue({ ok: true, status: 'ok', message: 'Connected' });
	updateSettingsSectionMock.mockResolvedValue({ status: 'ok' });
	listWalletsMock.mockResolvedValue({
		master: { address: '0x1111111111111111111111111111111111111111', perp_usd: 1000, spot_usd: 0 },
		registered: [],
		book_wallets: [],
		discovered: [],
		discovery_error: null,
		books: { enabled: false, long_only: false, long_book_configured: false, short_book_configured: false, named_wallets: [], subaccount_volume_requirement_usd: 0, note: null },
		master_wallet: '0x1111111111111111111111111111111111111111',
		can_transfer: false,
	});
	getCatalogMock.mockResolvedValue({ generated_at: '', total: 0, rows: [], facets: { stream: {}, venue: {}, tier: {}, state: {}, asset_class: {}, timeframe: {} } });
	listJobsMock.mockResolvedValue({ total: 0, jobs: [] });
	startDownloadsMock.mockResolvedValue({
		jobs: [
			{
				id: 'job-1',
				kind: 'download',
				title: 'BTC/USDT 1h (canonical)',
				origin: 'user',
				status: 'running',
				lane: 'user',
				routine: false,
				params: {},
				series: [],
				progress: { done: 50, total: 100, unit: 'bars' },
				message: null,
				result: null,
				error: null,
				attempts: 1,
				parent_id: null,
				cancel_requested: false,
				retryable: false,
				created_at: '',
				started_at: '',
				finished_at: null,
				updated_at: '',
			},
		],
	});
	gotoMock.mockResolvedValue(undefined);

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

function mountWizard(props: Record<string, unknown> = { settings: {} }): void {
	target = document.createElement('div');
	document.body.appendChild(target);
	instance = mount(SetupWizardModal, { target, props });
}

describe('SetupWizardModal (revamped guided onboarding)', () => {
	it('renders the welcoming safety sandbox and 4-stage pipeline on step 1', async () => {
		mountWizard();
		await flush();

		const text = target.textContent || '';
		expect(text).toContain('Welcome to Forven');
		expect(text).toContain('safe sandbox by default');
		expect(text).toContain('Backtest & Gauntlet');
		expect(text).toContain('Paper Trading');
		expect(text).toContain('Live (Opt-in)');
	});

	it('defaults to Paper Trading on step 2 with zero warnings and advances without window.confirm', async () => {
		const confirmSpy = vi.spyOn(window, 'confirm');
		mountWizard();
		wizardStep.set(1);
		await flush();

		const text = target.textContent || '';
		expect(text).toContain('Start with Paper Trading');
		expect(text).toContain('Paper Trading is ready — no exchange keys required');
		expect(text).not.toContain('⚠');

		const nextBtn = target.querySelector('[data-testid="wizard-next-btn"]') as HTMLButtonElement;
		expect(nextBtn).toBeTruthy();
		expect(nextBtn.textContent).toContain('Continue with Paper Trading');
		nextBtn.click();
		await flush();

		expect(confirmSpy).not.toHaveBeenCalled();
		expect(target.querySelector('[data-testid="wizard-step-ai"]')).toBeTruthy();
		confirmSpy.mockRestore();
	});

	it('reveals the guided Hyperliquid API wallet walkthrough when opted in and saves + verifies', async () => {
		const onSettingsUpdated = vi.fn();
		mountWizard({ settings: {}, onSettingsUpdated });
		wizardStep.set(1);
		await flush();

		const hlOption = target.querySelector('[data-testid="trading-mode-hyperliquid"]') as HTMLButtonElement;
		hlOption.click();
		await flush();

		const text = target.textContent || '';
		expect(text).toContain('How to create a safe Hyperliquid API wallet');
		expect(text).toContain('never withdraw funds');

		const inputs = Array.from(target.querySelectorAll('input')) as HTMLInputElement[];
		const walletInput = inputs[0];
		const keyInput = target.querySelector('#wizard-hl-key') as HTMLInputElement;

		walletInput.value = '0x1111111111111111111111111111111111111111';
		walletInput.dispatchEvent(new Event('input', { bubbles: true }));
		keyInput.value = '0x' + 'a'.repeat(64);
		keyInput.dispatchEvent(new Event('input', { bubbles: true }));
		await flush();

		const form = target.querySelector('[data-testid="hyperliquid-guided-form"] form') as HTMLFormElement;
		form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
		await flush();

		expect(updateSettingsSectionMock).toHaveBeenCalledWith('hyperliquid', {
			actual_wallet_address: '0x1111111111111111111111111111111111111111',
			use_testnet: true,
			api_secret_key: '0x' + 'a'.repeat(64),
		});
		expect(onSettingsUpdated).toHaveBeenCalled();
		expect(listWalletsMock).toHaveBeenCalledWith(false);
		expect(target.textContent).toContain('Connected to Hyperliquid (Testnet)');
	});

	it('shows popular AI providers first and folds extra providers behind a toggle', async () => {
		mountWizard();
		wizardStep.set(2);
		await flush();

		expect(target.querySelector('[data-testid="wizard-provider-card-openai"]')).toBeTruthy();
		expect(target.querySelector('[data-testid="wizard-provider-card-anthropic"]')).toBeTruthy();
		expect(target.querySelector('[data-testid="wizard-provider-card-gemini"]')).toBeTruthy();
		expect(target.textContent).toContain('Show 1 more provider');
		expect(target.textContent).not.toContain('Low-cost reasoning');
	});

	it('offers a 1-click starter market data download when catalog is empty', async () => {
		mountWizard();
		wizardStep.set(3);
		await flush();

		const text = target.textContent || '';
		expect(text).toContain('Download a starter market data pack');

		const downloadBtn = target.querySelector('[data-testid="download-starter-btn"]') as HTMLButtonElement;
		expect(downloadBtn).toBeTruthy();
		expect(downloadBtn.textContent).toContain('6 series');

		downloadBtn.click();
		await flush();

		expect(startDownloadsMock).toHaveBeenCalledTimes(1);
		const items = startDownloadsMock.mock.calls[0][0];
		expect(items).toHaveLength(6);
		expect(items[0]).toMatchObject({
			symbol: 'BTC-USDT',
			timeframe: '1h',
			venue: 'canonical',
			history: { mode: 'days', days: 730 },
		});
		expect(target.querySelector('[data-testid="data-jobs-list"]')).toBeTruthy();
		expect(target.textContent).toContain('BTC/USDT 1h (canonical)');
	});

	it('uses a calm inline confirmation strip (not window.confirm) when skipping without AI', async () => {
		const confirmSpy = vi.spyOn(window, 'confirm');
		mountWizard();
		await flush();

		const buttons = Array.from(target.querySelectorAll('button')) as HTMLButtonElement[];
		const skipBtn = buttons.find((b) => (b.textContent || '').includes('Skip setup'));
		expect(skipBtn).toBeTruthy();
		skipBtn!.click();
		await flush();

		expect(confirmSpy).not.toHaveBeenCalled();
		expect(target.querySelector('[data-testid="wizard-skip-confirm"]')).toBeTruthy();
		expect(updateSettingsSectionMock).not.toHaveBeenCalled();

		const confirmYes = target.querySelector('[data-testid="wizard-skip-confirm-yes"]') as HTMLButtonElement;
		confirmYes.click();
		await flush();

		expect(updateSettingsSectionMock).toHaveBeenCalledWith(
			'ui',
			expect.objectContaining({ setup_wizard_completed_at: expect.any(String) }),
		);
		confirmSpy.mockRestore();
	});

	it('reconciles agent providers, saves completion, and launches chosen route from the Ready step', async () => {
		getForvenAuthProvidersMock.mockResolvedValue({
			providers: [
				{ provider: 'openai', status: 'active', configured: true, supports_oauth: true, requires_token: true },
			],
		});
		mountWizard();
		wizardStep.set(4);
		await flush();

		expect(target.textContent).toContain('Your Forven workspace is ready!');
		expect(target.textContent).toContain('Connected: OpenAI');

		const forgeBtn = target.querySelector('[data-testid="wizard-launch-lab"]') as HTMLButtonElement;
		expect(forgeBtn).toBeTruthy();
		forgeBtn.click();
		await flush();

		expect(reconcileAgentProvidersMock).toHaveBeenCalledTimes(1);
		expect(updateSettingsSectionMock).toHaveBeenCalledWith(
			'ui',
			expect.objectContaining({ setup_wizard_completed_at: expect.any(String) }),
		);
		expect(gotoMock).toHaveBeenCalledWith('/lab');
	});
});
