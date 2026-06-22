/**
 * Safe markdown rendering for assistant output.
 *
 * Assistant/agent/scraped-derived text is rendered with `{@html}`, so it MUST be
 * sanitized — an XSS here runs in the app origin and can read the API/operator
 * keys from localStorage and drive /api/*. The old hand-rolled denylist had
 * verified bypasses (entity-encoded `javascript:` in href, `formaction`,
 * `xlink:href`), so we now run output through DOMPurify (allowlist-based, handles
 * entity/control-char obfuscation) in the browser, with a conservative regex
 * fallback only for SSR where no DOM is available (audit 2026-06-22, M3).
 */
import { marked } from 'marked';
import DOMPurify from 'dompurify';

const DANGEROUS_TAGS = ['script', 'style', 'iframe', 'object', 'embed', 'link', 'meta', 'form', 'base'];

const URL_ATTRS = new Set(['href', 'src', 'xlink:href', 'formaction', 'action', 'poster', 'ping', 'background']);

/** Strip control chars + whitespace a browser ignores inside a URL scheme. */
function stripUrlNoise(value: string): string {
	let out = '';
	for (const ch of value) {
		const code = ch.charCodeAt(0);
		if (code <= 0x20 || code === 0xa0 || code === 0x2028 || code === 0x2029) continue;
		out += ch;
	}
	return out;
}

function isSafeUrlValue(raw: string): boolean {
	const v = stripUrlNoise(raw || '').toLowerCase();
	if (v === '') return true;
	if (v.startsWith('#') || v.startsWith('/') || v.startsWith('./') || v.startsWith('../')) return true;
	return /^(https?:|mailto:|tel:)/.test(v);
}

function sanitizeWithDompurify(html: string): string {
	return DOMPurify.sanitize(html, {
		USE_PROFILES: { html: true },
		// DOMPurify strips javascript:/data:/vbscript: URIs (incl. entity/whitespace
		// obfuscation) and event handlers by default; these just tighten further.
		ALLOW_DATA_ATTR: false,
		ADD_ATTR: ['target'],
		FORBID_TAGS: DANGEROUS_TAGS,
		FORBID_ATTR: ['style', 'formaction', 'action', 'ping', 'srcset']
	});
}

function sanitizeFallback(html: string): string {
	// SSR / no-DOM only. Best-effort; the security-critical client path uses DOMPurify.
	if (typeof DOMParser !== 'undefined') {
		try {
			const doc = new DOMParser().parseFromString(html, 'text/html');
			doc.querySelectorAll(DANGEROUS_TAGS.join(',')).forEach((n) => n.remove());
			doc.querySelectorAll('*').forEach((el) => {
				for (const attr of Array.from(el.attributes)) {
					const name = attr.name.toLowerCase();
					if (name.startsWith('on')) {
						el.removeAttribute(attr.name);
						continue;
					}
					// DOM gives entity-decoded values; match against a scheme allowlist.
					if (URL_ATTRS.has(name) && !isSafeUrlValue(attr.value || '')) {
						el.removeAttribute(attr.name);
					}
				}
			});
			return doc.body.innerHTML;
		} catch {
			// fall through to regex
		}
	}
	return html
		.replace(/<\/?(?:script|style|iframe|object|embed|link|meta|form|base)\b[^>]*>/gi, '')
		.replace(/\son\w+\s*=\s*"[^"]*"/gi, '')
		.replace(/\son\w+\s*=\s*'[^']*'/gi, '')
		.replace(/(?:javascript|vbscript|data):/gi, '');
}

function sanitizeHtml(html: string): string {
	if (typeof window !== 'undefined' && typeof DOMPurify?.sanitize === 'function') {
		try {
			return sanitizeWithDompurify(html);
		} catch {
			// fall through to the no-dep fallback
		}
	}
	return sanitizeFallback(html);
}

export function renderMarkdown(src: string): string {
	const raw = src ?? '';
	let html: string;
	try {
		html = marked.parse(raw, { async: false }) as string;
	} catch {
		// If markdown parsing fails, fall back to escaped plain text.
		const div = typeof document !== 'undefined' ? document.createElement('div') : null;
		if (div) {
			div.textContent = raw;
			return div.innerHTML;
		}
		return raw.replace(/[<>&]/g, (c) => ({ '<': '&lt;', '>': '&gt;', '&': '&amp;' }[c] as string));
	}
	return sanitizeHtml(html);
}
