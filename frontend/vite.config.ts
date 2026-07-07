import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';
import { readFileSync } from 'fs';
import { resolve } from 'path';

const isVitest = process.env.VITEST === 'true';

// Read parent .env manually (Vite's envDir only feeds import.meta.env, not
// config-level process.env).
const _parentEnv: Record<string, string> = {};
try {
	const raw = readFileSync(resolve(process.cwd(), '../.env'), 'utf-8');
	for (const line of raw.split('\n')) {
		const t = line.trim();
		if (!t || t.startsWith('#')) continue;
		const i = t.indexOf('=');
		if (i === -1) continue;
		let v = t.slice(i + 1).trim();
		if ((v.startsWith('"') && v.endsWith('"')) || (v.startsWith("'") && v.endsWith("'"))) v = v.slice(1, -1);
		_parentEnv[t.slice(0, i).trim()] = v;
	}
} catch { /* .env may not exist in CI/test */ }
const backendOrigin =
	_parentEnv.FORVEN_CLIENT_BASE ||
	_parentEnv.FORVEN_API_ORIGIN ||
	`http://127.0.0.1:${_parentEnv.FORVEN_PORT ?? '8003'}`;

export default defineConfig({
	plugins: [sveltekit()],
	envDir: '..',
	// Pre-bundle deps that are only imported lazily (e.g. `marked` in AIChatPanel)
	// so Vite doesn't discover them MID-PAGE-LOAD and trigger a re-optimization —
	// that changes chunk hashes and 404s the already-loaded tab (the blank-white
	// screen seen on restart). Listing them here forces pre-bundling at startup.
	optimizeDeps: { include: ['marked'] },
	resolve: isVitest
		? {
			conditions: ['browser']
		}
		: undefined,
	build: {
		// ECharts is intentionally bundled as a dedicated heavy chunk.
		chunkSizeWarningLimit: 1200,
		rollupOptions: {
			onwarn(warning, warn) {
				const code = typeof warning === 'string' ? '' : warning.code;
				const message = typeof warning === 'string' ? warning : String(warning.message || '');
				const sourceId = typeof warning === 'string' ? '' : String((warning as { id?: string }).id || '');
				const isKnownSvelteRuntimeExportNoise =
					code === 'MISSING_EXPORT' &&
					sourceId.includes('@sveltejs/kit/src/runtime/client/client.js') &&
					/(untrack|fork|settled)/.test(message);

				if (isKnownSvelteRuntimeExportNoise) return;
				warn(warning);
			},
			output: {
				manualChunks(id) {
					if (id.includes('node_modules/echarts')) return 'vendor-echarts';
					if (id.includes('node_modules/lightweight-charts')) return 'vendor-lightweight-charts';
				}
			}
		}
	},
	server: {
		// Allow access via reverse proxy domains (fv.fusemob.com, api.fv.fusemob.com)
		// plus any entries from FORVEN_ALLOWED_HOSTS.
		allowedHosts: [
			'fv.fusemob.com',
			'api.fv.fusemob.com',
			...(_parentEnv.FORVEN_ALLOWED_HOSTS || '').split(',').map(h => h.trim()).filter(Boolean),
		],
		proxy: {
			'/api': {
				target: backendOrigin,
				changeOrigin: true,
				ws: true
			},
			'/health': {
				target: backendOrigin,
				changeOrigin: true
			}
		}
	},
	test: {
		include: ['src/**/*.{test,spec}.{js,ts}'],
		environment: 'jsdom',
		globals: true,
		setupFiles: ['./src/tests/setup.ts'],
		coverage: {
			reporter: ['text', 'json', 'html'],
			exclude: [
				'node_modules/',
				'src/tests/',
				'**/*.d.ts',
				'**/*.config.*',
				'.svelte-kit/'
			]
		}
	}
});
