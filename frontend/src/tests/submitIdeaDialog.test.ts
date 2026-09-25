import { afterEach, expect, it, vi } from 'vitest';
import { mount, unmount, tick } from 'svelte';
import { fireEvent } from '@testing-library/svelte';

const api = vi.hoisted(() => ({ submitIdea: vi.fn(), previewIdeaUrl: vi.fn(), getIdea: vi.fn() }));
vi.mock('$lib/api/ideas', () => api);
vi.mock('$lib/api/data', () => ({ getSymbols: vi.fn().mockResolvedValue(['BTC/USDT']) }));

import SubmitIdeaDialog from '$lib/components/lab/SubmitIdeaDialog.svelte';

let app: ReturnType<typeof mount> | null = null;
let target: HTMLDivElement;

async function settle(): Promise<void> {
	for (let i = 0; i < 8; i++) {
		await Promise.resolve();
		await tick();
	}
}

function setup(): void {
	target = document.createElement('div');
	document.body.append(target);
	app = mount(SubmitIdeaDialog, { target, props: { open: true } });
}

function button(label: string): HTMLButtonElement {
	return [...target.querySelectorAll('button')].find((b) => b.textContent?.trim() === label)!;
}

afterEach(async () => {
	if (app) await unmount(app);
	app = null;
	target?.remove();
	vi.clearAllMocks();
});

it('needs an idea or a URL before it can submit', async () => {
	setup();
	await settle();
	expect(button('Submit idea').disabled).toBe(true);
	await fireEvent.input(target.querySelector('textarea')!, { target: { value: 'Fade funding spikes on SOL' } });
	await settle();
	expect(button('Submit idea').disabled).toBe(false);
});

it('submits the idea with split markets and shows the queued task', async () => {
	api.submitIdea.mockResolvedValueOnce({ ok: true, task_id: 42 });
	setup();
	await settle();
	await fireEvent.input(target.querySelector('textarea')!, { target: { value: 'Fade funding spikes on SOL' } });
	const inputs = target.querySelectorAll('input');
	await fireEvent.input(inputs[1], { target: { value: 'SOL/USDT, ETH/USDT' } });
	await settle();
	await fireEvent.click(button('Submit idea'));
	await settle();
	expect(api.submitIdea).toHaveBeenCalledWith({
		text: 'Fade funding spikes on SOL',
		url: undefined,
		target_assets: ['SOL/USDT', 'ETH/USDT'],
		target_timeframes: undefined,
		notes: undefined,
	});
	expect(target.textContent).toContain('T42');
});

it('shows the backend error and keeps the form when the submit is refused', async () => {
	api.submitIdea.mockResolvedValueOnce({ ok: false, error: 'Could not read that URL.' });
	setup();
	await settle();
	await fireEvent.input(target.querySelectorAll('input')[0], { target: { value: 'https://example.com/post' } });
	await settle();
	await fireEvent.click(button('Submit idea'));
	await settle();
	expect(target.textContent).toContain('Could not read that URL.');
	expect(button('Submit idea')).toBeTruthy();
});
