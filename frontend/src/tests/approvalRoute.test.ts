import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

const api = vi.hoisted(() => ({
	approveApproval: vi.fn(),
	bulkApproveApprovals: vi.fn(),
	classifyApproval: vi.fn(),
	denyApproval: vi.fn(),
	getApprovalContext: vi.fn(),
	getApprovalModes: vi.fn(),
	getApprovals: vi.fn(),
	getSettings: vi.fn(),
	reviseApproval: vi.fn(),
	troubleshootApproval: vi.fn(),
	updateSettingsSection: vi.fn(),
	userCompleteApproval: vi.fn(),
}));
const appState = vi.hoisted(() => ({ url: new URL('http://localhost/approval') }));

vi.mock('$lib/api/forven', () => api);
vi.mock('$lib/api/skills', () => ({ getSkill: vi.fn() }));
vi.mock('$app/navigation', () => ({ goto: vi.fn() }));
vi.mock('$app/stores', () => ({
	page: {
		subscribe(callback: (value: { url: URL }) => void) {
			callback({ url: appState.url });
			return () => {};
		},
	},
}));

import ApprovalPage from '../routes/approval/+page.svelte';

const livePromotion = {
	id: 7,
	approval_type: 'strategy_promotion_approval',
	target_type: 'strategy',
	target_id: 'S00042',
	// The live target lives only in the payload: the old page missed this case.
	requested_status: null,
	status: 'pending_approval',
	actor: 'brain',
	reason: 'Paper soak passed',
	payload: { strategy_id: 'S00042', recommended_target_stage: 'live_graduated' },
	feedback: null,
	decision: null,
	error: null,
	owner: 'operator',
	created_at: '2026-10-05T10:00:00Z',
	updated_at: '2026-10-05T10:00:00Z',
	decided_at: null,
	requires_go_live: true,
};

let target: HTMLElement;
let instance: ReturnType<typeof mount> | null = null;

async function flush(): Promise<void> {
	for (let i = 0; i < 6; i += 1) {
		await Promise.resolve();
		await new Promise((resolve) => setTimeout(resolve, 0));
	}
	flushSync();
}

function button(label: string): HTMLButtonElement {
	const match = [...target.querySelectorAll('button')].find((el) => el.textContent?.trim() === label);
	if (!match) throw new Error(`button "${label}" not found`);
	return match as HTMLButtonElement;
}

function type(el: HTMLInputElement, value: string) {
	el.value = value;
	el.dispatchEvent(new Event('input', { bubbles: true }));
	flushSync();
}

beforeEach(() => {
	api.getApprovals.mockResolvedValue([livePromotion]);
	api.getApprovalModes.mockResolvedValue({ modes: {}, default_mode: 'manual' });
	api.getApprovalContext.mockResolvedValue({
		approval: livePromotion,
		strategy_context: { strategy: { symbol: 'BTC/USDT', timeframe: '1h', stage: 'paper', sharpe: 1.4 } },
		recommended_mode: 'diagnosis',
	});
	api.getSettings.mockResolvedValue({
		auto_approve_promotions: false,
		auto_approve_code_edits: false,
		promotion_mode: 'auto',
		hyperliquid_testnet: false,
		mainnet_armed: true,
	});
	api.approveApproval.mockResolvedValue({ ok: true });
	api.updateSettingsSection.mockResolvedValue({ status: 'ok' });
	target = document.createElement('div');
	document.body.appendChild(target);
});

afterEach(() => {
	if (instance) unmount(instance);
	instance = null;
	target.remove();
	vi.clearAllMocks();
});

describe('Approvals page', () => {
	it('shows promotions as automatic when the pipeline promotion mode is auto', async () => {
		instance = mount(ApprovalPage, { target });
		await flush();
		expect(target.querySelector('[data-testid="who-promotions"]')?.textContent).toContain('Automatic');
		expect(target.textContent).toContain('Set by Pipeline promotion mode');
		expect(target.querySelector('[data-testid="who-live"]')?.textContent).toContain('You type GO LIVE');
	});

	it('approving a payload-only live promotion opens the go-live dialog instead of approving', async () => {
		instance = mount(ApprovalPage, { target });
		await flush();
		button('Approve (go live)').click();
		await flush();

		expect(api.approveApproval).not.toHaveBeenCalled();
		const dialog = target.querySelector('[data-testid="go-live-dialog"]');
		expect(dialog).not.toBeNull();
		expect(target.querySelector('[data-testid="go-live-network"]')?.textContent).toContain('Mainnet');

		const confirm = target.querySelector('[data-testid="go-live-confirm"]') as HTMLButtonElement;
		expect(confirm.disabled).toBe(true);
		type(target.querySelector('[data-testid="go-live-ceiling"]') as HTMLInputElement, '500');
		type(target.querySelector('[data-testid="go-live-phrase"]') as HTMLInputElement, 'go live');
		expect(confirm.disabled).toBe(false);
		confirm.click();
		await flush();

		expect(api.approveApproval).toHaveBeenCalledWith(7, expect.objectContaining({
			confirm: 'GO LIVE',
			live_notional_ceiling_usd: 500,
		}));
		expect(target.querySelector('[data-testid="go-live-dialog"]')).toBeNull();
	});

	it('turning auto promotions off also switches the pipeline mode to manual', async () => {
		instance = mount(ApprovalPage, { target });
		await flush();
		(target.querySelector('[data-testid="toggle-auto-promotions"]') as HTMLButtonElement).click();
		await flush();
		(target.querySelector('[data-testid="confirm-dialog-go"]') as HTMLButtonElement).click();
		await flush();

		expect(api.updateSettingsSection).toHaveBeenCalledWith('bot-operations', { auto_approve_promotions: 'false' });
		expect(api.updateSettingsSection).toHaveBeenCalledWith('pipeline', { promotion_mode: 'manual' });
		expect(target.querySelector('[data-testid="who-promotions"]')?.textContent).toContain('You approve');
	});
});
