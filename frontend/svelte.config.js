import adapterAuto from '@sveltejs/adapter-auto';
import adapterStatic from '@sveltejs/adapter-static';
import { vitePreprocess } from '@sveltejs/vite-plugin-svelte';
import { readFileSync } from 'fs';
import { resolve, dirname } from 'path';
import { fileURLToPath } from 'url';

const usePackaged = process.env.FORVEN_PACKAGE_BUILD === '1';

// Read parent .env so CSP connect-src includes the public host/port.
const __dirname = dirname(fileURLToPath(import.meta.url));
const _parentEnv = {};
try {
	const raw = readFileSync(resolve(__dirname, '../.env'), 'utf-8');
	for (const line of raw.split('\n')) {
		const t = line.trim();
		if (!t || t.startsWith('#')) continue;
		const i = t.indexOf('=');
		if (i === -1) continue;
		let v = t.slice(i + 1).trim();
		if ((v.startsWith('"') && v.endsWith('"')) || (v.startsWith("'") && v.endsWith("'"))) v = v.slice(1, -1);
		_parentEnv[t.slice(0, i).trim()] = v;
	}
} catch { /* .env may not exist */ }

// Parse allowed hosts for CSP connect-src
const allowedHosts = (_parentEnv.FORVEN_ALLOWED_HOSTS || '').split(',').map(h => h.trim()).filter(Boolean);
const connectExtra = allowedHosts.flatMap(host => [
	`http://${host}:*`,
	`ws://${host}:*`,
]);

/** @type {import('@sveltejs/kit').Config} */
const config = {
	preprocess: vitePreprocess(),

	kit: {
		adapter: usePackaged
			? adapterStatic({ pages: 'build', assets: 'build', fallback: 'index.html', strict: false })
			: adapterAuto(),
		prerender: {
			handleUnseenRoutes: 'warn'
		},
		// SECURITY (audit 2026-06-22, M5): a Content-Security-Policy is the
		// defense-in-depth backstop for the localStorage-resident API/operator
		// keys — any in-origin script execution (a future DOM-XSS, a malicious
		// extension) is otherwise full authenticated API access + key theft.
		// script-src 'self' (SvelteKit hashes its own bootstrap) blocks injected
		// inline/remote scripts; styles stay unsafe-inline so charts/Tailwind keep
		// working; connect-src is scoped to the local API + the Binance market WS.
		// Allowed hosts are injected from FORVEN_ALLOWED_HOSTS in parent .env
		// so the frontend works from any public IP / domain without CSP errors.
		csp: {
			mode: 'hash',
			directives: {
				'default-src': ['self'],
				'script-src': ['self'],
				'style-src': ['self', 'unsafe-inline'],
				'img-src': ['self', 'data:', 'blob:', 'https:'],
				'font-src': ['self', 'data:'],
				'connect-src': [
					'self',
					'http://localhost:*',
					'http://127.0.0.1:*',
					'ws://localhost:*',
					'ws://127.0.0.1:*',
					'wss://stream.binance.com:9443',
					...connectExtra,
				],
				'object-src': ['none'],
				'base-uri': ['self'],
				'frame-ancestors': ['none'],
				'form-action': ['self']
			}
		}
	}
};

export default config;
