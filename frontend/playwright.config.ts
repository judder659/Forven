import { defineConfig } from 'playwright/test';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const baseURL = 'http://127.0.0.1:4173';

// The e2e backend is a REAL `forven.api` boot — same process CI ships — but:
// - on its own port so it never collides with a dev instance on 8003,
// - with FORVEN_HOME pointed at a throwaway temp dir so it can never read or
//   write a real install's DB/config,
// - control-plane only (no scheduler/agent/brain/daemon loops) so runs are
//   deterministic and network-quiet.
const backendPort = 8017;
const backendOrigin = `http://127.0.0.1:${backendPort}`;
const e2eHome = mkdtempSync(join(tmpdir(), 'forven-e2e-home-'));

export default defineConfig({
	testDir: './e2e',
	fullyParallel: false,
	retries: process.env.CI ? 1 : 0,
	timeout: 30_000,
	use: {
		baseURL,
		trace: 'on-first-retry',
	},
	projects: [
		{
			name: 'chromium',
			use: {
				browserName: 'chromium',
			},
		},
	],
	webServer: [
		{
			command: 'python -m forven.api',
			cwd: '..',
			url: `${backendOrigin}/health`,
			reuseExistingServer: false,
			timeout: 180_000,
			env: {
				FORVEN_PORT: String(backendPort),
				FORVEN_HOME: e2eHome,
				FORVEN_API_CONTROL_PLANE_ONLY: '1',
			},
		},
		{
			command: 'npm run dev -- --host 127.0.0.1 --port 4173',
			url: `${baseURL}/`,
			reuseExistingServer: !process.env.CI,
			timeout: 120_000,
			env: {
				FORVEN_API_ORIGIN: backendOrigin,
			},
		},
	],
});
