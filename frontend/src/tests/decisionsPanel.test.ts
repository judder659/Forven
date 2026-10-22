import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

const api = vi.hoisted(() => ({
	getApprovals: vi.fn(),
	approveApproval: vi.fn(),
	denyApproval: vi.fn(),
}));

vi.mock('$lib/api/forven', async (importOriginal) => ({
	...(await importOriginal<Record<string, unknown>>()),
	...api,
}));

import DecisionsPanel from '../lib/components/approvals/DecisionsPanel.svelte';

let target: HTMLElement;
let instance: ReturnType<typeof mount> | null = null;

async function flush(): Promise<void> {
	for (let i = 0; i < 4; i += 1) {
		await Promise.resolve();
		await new Promise((resolve) => setTimeout(resolve, 0));
	}
	flushSync();
}

function approval(overrides: Record<string, unknown>) {
	return {
		id: 1,
		approval_type: 'code_change',
		target_type: 'task',
		target_id: 't-1',
		requested_status: null,
		status: 'pending_approval',
		actor: 'brain',
		reason: 'Tighten the funding filter',
		payload: {},
		feedback: null,
		decision: null,
		error: null,
		owner: 'ceo',
		created_at: new Date().toISOString(),
		updated_at: new Date().toISOString(),
		decided_at: null,
		...overrides,
	};
}

async function render(rows: unknown[]): Promise<void> {
	api.getApprovals.mockResolvedValue(rows);
	target = document.createElement('div');
	document.body.appendChild(target);
	instance = mount(DecisionsPanel, { target, props: {} });
	await flush();
}

function button(testid: string): HTMLButtonElement {
	return target.querySelector(`[data-testid="${testid}"]`) as HTMLButtonElement;
}

beforeEach(() => {
	api.approveApproval.mockResolvedValue({});
	api.denyApproval.mockResolvedValue({});
});

afterEach(() => {
	if (instance) unmount(instance);
	instance = null;
	target?.remove();
	vi.clearAllMocks();
});

describe('DecisionsPanel', () => {
	it('renders nothing when no decision is waiting', async () => {
		await render([]);
		expect(target.querySelector('[data-testid="decisions-panel"]')).toBeNull();
		expect(api.getApprovals).toHaveBeenCalledWith({ status: 'pending_approval', limit: 100 });
	});

	it('approves a code change inline and reloads the list', async () => {
		await render([approval({})]);
		expect(target.textContent).toContain('Code Change');
		expect(target.textContent).toContain('Tighten the funding filter');

		api.getApprovals.mockResolvedValue([]);
		button('decision-approve').click();
		await flush();

		expect(api.approveApproval).toHaveBeenCalledWith(1, expect.objectContaining({ actor: 'operator' }));
		expect(target.querySelector('[data-testid="decision-item"]')).toBeNull();
		expect(target.querySelector('[data-testid="decisions-message"]')).not.toBeNull();
	});

	it('names a live demotion request Move / Keep, and Keep asks for a reason', async () => {
		await render([
			approval({
				id: 7,
				approval_type: 'strategy_dethrone_recommendation',
				target_type: 'strategy',
				target_id: 'S01566',
				requested_status: 'paper',
				payload: { current_stage: 'live_graduated' },
			}),
		]);

		expect(button('decision-approve').textContent).toContain('Move to');
		expect(button('decision-deny').textContent).toContain('Keep');

		button('decision-deny').click();
		await flush();
		expect(api.denyApproval).not.toHaveBeenCalled();
		expect(target.textContent).toContain('Performing fine');
	});

	it('sends a go-live approval through the typed GO LIVE dialog', async () => {
		await render([
			approval({
				id: 9,
				approval_type: 'strategy_promotion_approval',
				target_type: 'strategy',
				target_id: 'S02000',
				requested_status: 'live_graduated',
				requires_go_live: true,
			}),
		]);

		expect(button('decision-approve').textContent).toContain('Go live');
		button('decision-approve').click();
		await flush();

		expect(api.approveApproval).not.toHaveBeenCalled();
		expect(document.body.textContent).toContain('GO LIVE');
	});
});
