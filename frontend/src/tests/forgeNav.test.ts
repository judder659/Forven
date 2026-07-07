import { describe, it, expect, beforeEach } from 'vitest';
import { setForgeNavList, getForgeNavPosition } from '$lib/stores/forgeNav';

const LIST = [
	{ id: 's-1', label: 'Alpha' },
	{ id: 's-2', label: 'Bravo' },
	{ id: 's-3', label: 'Charlie' },
];

describe('forge container navigation context', () => {
	beforeEach(() => {
		sessionStorage.clear();
	});

	it('walks the stored list order', () => {
		setForgeNavList(LIST);
		const pos = getForgeNavPosition('s-2');
		expect(pos).not.toBeNull();
		expect(pos?.index).toBe(1);
		expect(pos?.total).toBe(3);
		expect(pos?.prev?.id).toBe('s-1');
		expect(pos?.next?.id).toBe('s-3');
	});

	it('clamps at the ends instead of wrapping', () => {
		setForgeNavList(LIST);
		expect(getForgeNavPosition('s-1')?.prev).toBeNull();
		expect(getForgeNavPosition('s-1')?.next?.id).toBe('s-2');
		expect(getForgeNavPosition('s-3')?.next).toBeNull();
	});

	it('returns null for a container not opened from the list (deep link)', () => {
		setForgeNavList(LIST);
		expect(getForgeNavPosition('s-elsewhere')).toBeNull();
	});

	it('returns null with no stored context at all', () => {
		expect(getForgeNavPosition('s-1')).toBeNull();
	});

	it('drops malformed entries and survives corrupted storage', () => {
		setForgeNavList([
			{ id: 's-1', label: 'Alpha' },
			{ id: '', label: 'no id' },
			// @ts-expect-error deliberately malformed
			null,
			// @ts-expect-error label of the wrong type must be coerced, not crash
			{ id: 's-2', label: 42 },
		]);
		const pos = getForgeNavPosition('s-2');
		expect(pos?.total).toBe(2);
		expect(pos?.prev?.label).toBe('Alpha');

		sessionStorage.setItem('forven:forge:nav-list', '{not json');
		expect(getForgeNavPosition('s-1')).toBeNull();
	});

	it('a later capture replaces the earlier one', () => {
		setForgeNavList(LIST);
		setForgeNavList([{ id: 's-9', label: 'Zulu' }]);
		expect(getForgeNavPosition('s-2')).toBeNull();
		expect(getForgeNavPosition('s-9')?.total).toBe(1);
	});
});
