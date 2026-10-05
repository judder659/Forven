import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

const api = vi.hoisted(() => ({
	listWallets: vi.fn(),
	createSubaccount: vi.fn(),
	registerWallet: vi.fn(),
	removeWallet: vi.fn(),
	transferWallet: vi.fn(),
	classTransfer: vi.fn(),
}));

vi.mock('$lib/api', () => api);
vi.mock('$lib/stores/processTracker', () => ({ addToast: vi.fn() }));

import WalletsManager from '../lib/components/settings/WalletsManager.svelte';

let target: HTMLElement;
let instance: ReturnType<typeof mount> | null = null;

async function flush(): Promise<void> {
	for (let i = 0; i < 4; i += 1) {
		await Promise.resolve();
		await new Promise((resolve) => setTimeout(resolve, 0));
	}
	flushSync();
}

beforeEach(() => {
	api.listWallets.mockResolvedValue({
		master: { address: '0x1234567890abcdef1234', perp_usd: 1000, spot_usd: 500 },
		registered: [],
		book_wallets: [],
		discovered: [],
		discovery_error: null,
		books: { enabled: false, long_only: false, long_book_configured: false, short_book_configured: false, named_wallets: [], subaccount_volume_requirement_usd: 0, note: null },
		master_wallet: '0x1234567890abcdef1234',
		can_transfer: true,
	});
	api.classTransfer.mockResolvedValue({ status: 'ok' });
	target = document.createElement('div');
	document.body.appendChild(target);
});

afterEach(() => {
	if (instance) unmount(instance);
	instance = null;
	target.remove();
	vi.clearAllMocks();
});

describe('WalletsManager fund moves', () => {
	it('confirms a spot to perp move on mainnet before sending it', async () => {
		instance = mount(WalletsManager, { target, props: { onMainnet: true } });
		await flush();
		expect(target.querySelector('[data-testid="wallets-network"]')?.textContent).toContain('Mainnet');

		[...target.querySelectorAll('button')].find((b) => b.textContent?.includes('Spot ⇄ Perp'))!.click();
		flushSync();
		const amount = target.querySelector('#class-amount-master') as HTMLInputElement;
		amount.value = '250';
		amount.dispatchEvent(new Event('input', { bubbles: true }));
		flushSync();
		[...target.querySelectorAll('button')].find((b) => b.textContent?.trim() === 'Send')!.click();
		await flush();

		expect(api.classTransfer).not.toHaveBeenCalled();
		const dialog = target.querySelector('[data-testid="confirm-dialog"]');
		expect(dialog?.textContent).toContain('Mainnet (real money)');
		(target.querySelector('[data-testid="confirm-dialog-go"]') as HTMLButtonElement).click();
		await flush();

		expect(api.classTransfer).toHaveBeenCalledWith(null, 250, true);
	});
});
