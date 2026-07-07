/**
 * Forge container navigation context.
 *
 * When the operator opens a strategy container from The Forge list, the list
 * page stores its CURRENT filtered+sorted order here (all pages, not just the
 * visible one). The container detail page reads it back to offer prev/next
 * arrows that walk the exact order the operator was looking at — filters,
 * search, and sort included.
 *
 * sessionStorage (not a Svelte store) so the context survives a detail-page
 * reload but never leaks across browser tabs or sessions. Deep links that
 * did not come through the list simply get no arrows.
 */

const STORAGE_KEY = 'forven:forge:nav-list';

export type ForgeNavEntry = { id: string; label: string };

export type ForgeNavPosition = {
	index: number; // 0-based position of the current container
	total: number;
	prev: ForgeNavEntry | null;
	next: ForgeNavEntry | null;
};

export function setForgeNavList(entries: ForgeNavEntry[]): void {
	try {
		const cleaned = entries
			.filter((e) => e && typeof e.id === 'string' && e.id.trim())
			.map((e) => ({ id: e.id, label: typeof e.label === 'string' ? e.label : '' }));
		sessionStorage.setItem(STORAGE_KEY, JSON.stringify(cleaned));
	} catch {
		// Storage unavailable/full — navigation arrows just won't render.
	}
}

function readList(): ForgeNavEntry[] {
	try {
		const raw = sessionStorage.getItem(STORAGE_KEY);
		if (!raw) return [];
		const parsed = JSON.parse(raw);
		if (!Array.isArray(parsed)) return [];
		return parsed
			.filter((e): e is ForgeNavEntry => !!e && typeof e.id === 'string' && e.id.trim().length > 0)
			.map((e) => ({ id: e.id, label: typeof e.label === 'string' ? e.label : '' }));
	} catch {
		return [];
	}
}

/** Position of `currentId` in the stored list, or null when the container was
 *  not opened from the list (deep link) or the context is gone. */
export function getForgeNavPosition(currentId: string): ForgeNavPosition | null {
	const id = String(currentId || '').trim();
	if (!id) return null;
	const list = readList();
	const index = list.findIndex((e) => e.id === id);
	if (index === -1) return null;
	return {
		index,
		total: list.length,
		prev: index > 0 ? list[index - 1] : null,
		next: index < list.length - 1 ? list[index + 1] : null,
	};
}
