// Close a popover when the pointer goes down outside it or Escape is pressed.
export function dismissable(node: HTMLElement, onDismiss: () => void) {
	let handler = onDismiss;
	const onPointer = (event: PointerEvent) => {
		if (!node.contains(event.target as Node)) handler();
	};
	const onKey = (event: KeyboardEvent) => {
		if (event.key === 'Escape') handler();
	};
	// Attach after the opening click finishes, or that click would close it again.
	const timer = setTimeout(() => {
		window.addEventListener('pointerdown', onPointer, true);
		window.addEventListener('keydown', onKey);
	});
	return {
		update(next: () => void) {
			handler = next;
		},
		destroy() {
			clearTimeout(timer);
			window.removeEventListener('pointerdown', onPointer, true);
			window.removeEventListener('keydown', onKey);
		},
	};
}
