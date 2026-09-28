// Render an overlay under <body>. Pages sit in a stacking context below the
// app sidebar, so a fixed overlay left inside the page is drawn under it.
// Use it on the single root element of an {#if} block or component.
export function portal(node: HTMLElement) {
	document.body.appendChild(node);
	return {
		destroy() {
			node.remove();
		},
	};
}
