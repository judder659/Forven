// Place a popover under its anchor (above it when there is no room below),
// kept inside the viewport. Fixed positioning keeps scroll containers from
// clipping it; it follows the anchor when a container scrolls.
export function anchored(node: HTMLElement, anchor: HTMLElement | undefined) {
	let current = anchor;
	const margin = 8;
	function place() {
		if (!current) return;
		const rect = current.getBoundingClientRect();
		const width = node.offsetWidth;
		const height = node.offsetHeight;
		const left = Math.max(margin, Math.min(rect.left, window.innerWidth - width - margin));
		let top = rect.bottom + 4;
		if (top + height > window.innerHeight - margin && rect.top - height - 4 > margin) top = rect.top - height - 4;
		node.style.position = 'fixed';
		node.style.left = `${left}px`;
		node.style.top = `${top}px`;
	}
	place();
	const resize = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(place);
	resize?.observe(node);
	window.addEventListener('resize', place);
	window.addEventListener('scroll', place, true);
	return {
		update(next: HTMLElement | undefined) {
			current = next;
			place();
		},
		destroy() {
			resize?.disconnect();
			window.removeEventListener('resize', place);
			window.removeEventListener('scroll', place, true);
		},
	};
}
