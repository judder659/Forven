import { describe, it, expect } from 'vitest';
import { renderMarkdown } from '$lib/utils/markdown';
import { safeHref, isSafeHref } from '$lib/utils/url';

// Audit 2026-06-22 (M3): the chat markdown sanitizer must neutralize the
// verified XSS bypasses. Runs under the vitest jsdom env, so the DOMPurify
// browser path is exercised.
describe('renderMarkdown sanitization', () => {
	const cases: Record<string, string> = {
		script_tag: '<script>window.__pwned=1</script>',
		img_onerror: '<img src=x onerror="window.__pwned=1">',
		anchor_js: '<a href="javascript:window.__pwned=1">x</a>',
		anchor_js_entity_tab: '<a href="jav&#x09;ascript:window.__pwned=1">x</a>',
		button_formaction: '<button formaction="javascript:window.__pwned=1">x</button>',
		svg_xlink: '<svg><a xlink:href="javascript:window.__pwned=1">x</a></svg>',
		iframe: '<iframe src="javascript:window.__pwned=1"></iframe>',
		md_js_link: '[click](javascript:window.__pwned=1)'
	};

	for (const [name, payload] of Object.entries(cases)) {
		it(`neutralizes ${name}`, () => {
			const out = renderMarkdown(payload).toLowerCase();
			expect(out).not.toContain('onerror');
			expect(out).not.toContain('<script');
			expect(out).not.toContain('<iframe');
			// No surviving javascript: in any attribute value.
			expect(out).not.toMatch(/javascript:/);
		});
	}

	it('keeps benign markdown intact', () => {
		const out = renderMarkdown('**bold** and [a link](https://example.com) and `code`');
		expect(out).toContain('<strong>');
		expect(out).toContain('href="https://example.com"');
		expect(out).toContain('<code>');
	});
});

describe('safeHref', () => {
	it('allows http/https/relative/anchor', () => {
		expect(safeHref('https://example.com')).toBe('https://example.com');
		expect(safeHref('/foo')).toBe('/foo');
		expect(isSafeHref('#a')).toBe(true);
	});
	it('blocks javascript and obfuscated variants', () => {
		expect(safeHref('javascript:alert(1)')).toBe('#');
		expect(safeHref('JAV\tASCRIPT:alert(1)')).toBe('#');
		expect(safeHref('data:text/html,<script>1</script>')).toBe('#');
		expect(isSafeHref('vbscript:msgbox(1)')).toBe(false);
	});
});
