/**
 * Fixed-row-height windowing for long lists: only the rows in view (plus an
 * overscan margin) are rendered; spacer heights keep the scrollbar honest.
 */
export interface RowWindow {
	/** First rendered index. */
	start: number;
	/** One past the last rendered index. */
	end: number;
	padTop: number;
	padBottom: number;
}

export function windowRange(scrollTop: number, viewportHeight: number, rowHeight: number, total: number, overscan = 8): RowWindow {
	if (total <= 0 || rowHeight <= 0) return { start: 0, end: 0, padTop: 0, padBottom: 0 };
	const first = Math.floor(Math.max(0, scrollTop) / rowHeight);
	const visible = Math.ceil(Math.max(0, viewportHeight) / rowHeight) + 1;
	const start = Math.max(0, Math.min(total - 1, first - overscan));
	const end = Math.min(total, first + visible + overscan);
	return { start, end, padTop: start * rowHeight, padBottom: (total - end) * rowHeight };
}

/** The scrollTop that brings `index` fully into view, or null when it already is. */
export function scrollTopFor(index: number, scrollTop: number, viewportHeight: number, rowHeight: number): number | null {
	const top = index * rowHeight;
	const bottom = top + rowHeight;
	if (top < scrollTop) return top;
	if (bottom > scrollTop + viewportHeight) return Math.max(0, bottom - viewportHeight);
	return null;
}

/** Keyboard movement within [0, total): arrows, page keys, Home and End. */
export function moveIndex(current: number, key: string, total: number, pageSize: number): number | null {
	if (total <= 0) return null;
	const at = current < 0 ? -1 : Math.min(current, total - 1);
	switch (key) {
		case 'ArrowDown':
			return Math.min(total - 1, at + 1);
		case 'ArrowUp':
			return Math.max(0, at < 0 ? 0 : at - 1);
		case 'PageDown':
			return Math.min(total - 1, Math.max(0, at) + pageSize);
		case 'PageUp':
			return Math.max(0, at - pageSize);
		case 'Home':
			return 0;
		case 'End':
			return total - 1;
		default:
			return null;
	}
}
