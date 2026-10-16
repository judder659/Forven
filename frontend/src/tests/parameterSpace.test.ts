import { afterEach, describe, expect, it } from 'vitest';
import { mount, tick, unmount } from 'svelte';

import ParameterSpace from '$lib/components/strategy/container/ParameterSpace.svelte';

let component: ReturnType<typeof mount> | null = null;

afterEach(() => {
	if (component) unmount(component);
	component = null;
	document.body.innerHTML = '';
});

async function renderRow(params: Record<string, unknown>, space: Record<string, unknown>, key: string) {
	component = mount(ParameterSpace, { target: document.body, props: { params, space } });
	await tick();
	return document.querySelector(`[data-testid="parameter-space-${key}"]`)?.textContent ?? '';
}

describe('ParameterSpace', () => {
	it('reads the tagged choice list imported strategies store', async () => {
		const text = await renderRow({ hold: 18 }, { hold: { values: [12, 18, 24] } }, 'hold');
		expect(text).toContain('12');
		expect(text).toContain('24');
		expect(text).toContain('inside its range');
	});

	it('expands a tagged range', async () => {
		const text = await renderRow({ win: 0.3 }, { win: { range: [0.1, 0.5, 0.1] } }, 'win');
		expect(text).toContain('0.3');
		expect(text).not.toContain('0.30000000000000004');
		expect(text).toContain('inside its range');
	});

	it('keeps plain lists and marks unsearched params fixed', async () => {
		expect(await renderRow({ fast: 8 }, { fast: [8, 12, 16] }, 'fast')).toContain('at the low edge');
		unmount(component!);
		component = null;
		expect(await renderRow({ slow: 26 }, {}, 'slow')).toContain('fixed (not searched)');
	});
});
