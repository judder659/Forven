import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vitest/config';

const backendOrigin =
	process.env.FORVEN_CLIENT_BASE ||
	process.env.FORVEN_API_ORIGIN ||
	`http://127.0.0.1:${process.env.FORVEN_PORT ?? '8003'}`;
const isVitest = process.env.VITEST === 'true';

export default defineConfig({
	plugins: [sveltekit()],
	// Prepare lazy UI dependencies together before serving a page. Discovering
	// charts or markdown dependencies later replaces chunks an open tab still needs.
	optimizeDeps: {
		include: ['marked', 'lightweight-charts', 'dompurify', 'mermaid', 'svelte-dnd-action']
	},
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
