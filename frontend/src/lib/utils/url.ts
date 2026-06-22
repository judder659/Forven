/**
 * URL safety helpers for the UI (audit 2026-06-22, M4).
 *
 * Agent/scraped-derived values (e.g. a hypothesis artifact's `source_ref`) get
 * bound to `<a href>`. An attacker-supplied `javascript:` URL would run in the
 * app origin on click and could exfiltrate the API/operator keys from
 * localStorage. `safeHref` returns the value only when its scheme is in a safe
 * allowlist (http/https/mailto/tel/relative/anchor), defeating entity- and
 * control-char-obfuscated `javascript:`/`data:` URLs; otherwise it returns '#'.
 */

function stripUrlNoise(value: string): string {
	let out = '';
	for (const ch of value) {
		const code = ch.charCodeAt(0);
		if (code <= 0x20 || code === 0xa0 || code === 0x2028 || code === 0x2029) continue;
		out += ch;
	}
	return out;
}

export function isSafeHref(raw: string | null | undefined): boolean {
	const v = stripUrlNoise(String(raw ?? '')).toLowerCase();
	if (v === '') return false;
	if (v.startsWith('#') || v.startsWith('/') || v.startsWith('./') || v.startsWith('../')) return true;
	return /^(https?:|mailto:|tel:)/.test(v);
}

export function safeHref(raw: string | null | undefined): string {
	return isSafeHref(raw) ? String(raw).trim() : '#';
}
