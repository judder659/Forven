/** Trap keyboard focus while mounted and return it to the invoking control. */
export function dialogFocus(node: HTMLElement) {
	const previous = document.activeElement as HTMLElement | null;
	const selector =
		'button:not(:disabled), input:not(:disabled), textarea:not(:disabled), select:not(:disabled), a[href], summary, [tabindex="0"]';
	const controls = () =>
		[...node.querySelectorAll<HTMLElement>(selector)].filter((el) => el.getClientRects().length > 0);
	queueMicrotask(() => controls()[0]?.focus());
	const keydown = (event: KeyboardEvent) => {
		if (event.key !== 'Tab') return;
		const items = controls();
		const first = items[0];
		const last = items.at(-1);
		if (!first) {
			event.preventDefault();
			return;
		}
		if (event.shiftKey && (document.activeElement === first || !node.contains(document.activeElement))) {
			event.preventDefault();
			last?.focus();
		} else if (!event.shiftKey && (document.activeElement === last || !node.contains(document.activeElement))) {
			event.preventDefault();
			first.focus();
		}
	};
	node.addEventListener('keydown', keydown);
	return {
		destroy() {
			node.removeEventListener('keydown', keydown);
			previous?.focus();
		},
	};
}
