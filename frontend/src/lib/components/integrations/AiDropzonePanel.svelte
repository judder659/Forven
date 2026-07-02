<script lang="ts">
	import { onDestroy, onMount } from 'svelte';
	import {
		getAiDropzoneContext,
		getAiDropzoneSession,
		listAiDropzoneSessions,
		createAiDropzoneSession,
		closeAiDropzoneSession,
		registerStrategyFile,
		getRecentIntake,
		getBacktestingRuns,
		checkHealth,
		type AiDropzoneContext,
		type AiDropzoneSession,
		type AiDropzoneSessionDetail,
		type IntakeRecentResponse,
		type BacktestingRunSummary,
	} from '$lib/api';
	import { fetchApi } from '$lib/api/core';
	import { scanStrategy, type AstReport } from '$lib/api/strategyGuard';

	const SESSION_STORAGE_KEY = 'forven:ai-dropzone:active-session-id';
	const REFRESH_MS = 5000;
	// Keep in sync with tests/test_mcp_server.py EXPECTED_TOOL_NAMES.
	const MCP_TOOL_COUNT = 20;

	// ── State ────────────────────────────────────────────────────────────
	let context: AiDropzoneContext | null = null;
	let backendOk = false;
	let loading = true;
	let error: string | null = null;
	let success: string | null = null;

	let sessions: AiDropzoneSession[] = [];
	let activeSessionId = '';
	let activeDetail: AiDropzoneSessionDetail | null = null;
	let sessionBusy = false;

	let showNewSessionForm = false;
	let newSessionLabel = '';
	let newSessionObjective = '';

	let recentIntake: IntakeRecentResponse | null = null;
	let recentRuns: BacktestingRunSummary[] = [];

	// Manual fallback — register file
	let registerFilePath = '';
	let registerBusy = false;
	// Sandbox AST gate (P2-T14): scan before register so an AI-generated
	// strategy that imports os/subprocess can never be installed silently.
	let scanReport: AstReport | null = null;
	let scannedPath = '';
	let scanBusy = false;
	let scanError: string | null = null;
	$: scanClean = scanReport ? scanReport.ok : false;
	$: scanStale = scanReport !== null && registerFilePath.trim() !== scannedPath;

	// Manual fallback — run backtest
	let backtestStrategyId = '';
	let backtestDatasetId = 'BTC/USDT-1h';
	let backtestBusy = false;

	let showMcpConfig = false;
	let showHttpConfig = false;

	// ── MCP client catalogue ─────────────────────────────────────────────
	// Most MCP clients accept the same `mcpServers` wrapper, but a handful
	// have their own shape (VS Code native uses `servers`, Zed uses
	// `context_servers`, Codex uses TOML, Continue nests under
	// `experimental`). The `format` field drives which serializer runs.
	type ConfigFormat =
		| 'mcp-servers-json' // Claude Desktop/Code, Cursor, Cline, Roo, Windsurf, Jan, Warp, Minimax, LibreChat
		| 'vscode-servers-json' // VS Code native MCP (Copilot)
		| 'zed-context-json' // Zed editor
		| 'continue-experimental-json' // Continue.dev
		| 'codex-toml'; // OpenAI Codex CLI
	type McpClient = {
		id: string;
		name: string;
		group: 'Desktop chat' | 'IDE / code editor' | 'CLI / terminal' | 'Other';
		paths: string[];
		format: ConfigFormat;
		note?: string;
	};
	const MCP_CLIENTS: McpClient[] = [
		// ── Desktop chat apps ───────────────────────────────────
		{
			id: 'claude-desktop',
			name: 'Claude Desktop',
			group: 'Desktop chat',
			format: 'mcp-servers-json',
			paths: [
				'Windows: %APPDATA%\\Claude\\claude_desktop_config.json',
				'macOS: ~/Library/Application Support/Claude/claude_desktop_config.json',
			],
		},
		{
			id: 'chatgpt-desktop',
			name: 'ChatGPT Desktop (MCP connectors)',
			group: 'Desktop chat',
			format: 'mcp-servers-json',
			paths: ['Settings → Connectors → Add custom → MCP server'],
			note: 'ChatGPT Desktop accepts the same command/args/env triple via its Connectors UI.',
		},
		{
			id: 'jan',
			name: 'Jan',
			group: 'Desktop chat',
			format: 'mcp-servers-json',
			paths: ['Settings → Model Context Protocol (writes to ~/.jan/mcp.json)'],
		},
		{
			id: 'librechat',
			name: 'LibreChat',
			group: 'Desktop chat',
			format: 'mcp-servers-json',
			paths: ['librechat.yaml → mcpServers: (same JSON shape under the YAML key)'],
		},
		{
			id: 'minimax',
			name: 'Minimax Agent',
			group: 'Desktop chat',
			format: 'mcp-servers-json',
			paths: ['Agent Settings → MCP Servers → Import JSON'],
			note: 'Minimax Agent accepts the standard mcpServers JSON — paste the snippet into the import dialog.',
		},

		// ── IDE / code editor ───────────────────────────────────
		{
			id: 'claude-code',
			name: 'Claude Code',
			group: 'IDE / code editor',
			format: 'mcp-servers-json',
			paths: [
				'Global: ~/.claude.json (mcpServers section)',
				'Per-project: .mcp.json at the repo root',
			],
			note: 'Or run: claude mcp add forven -- python -m forven.mcp_server',
		},
		{
			id: 'cursor',
			name: 'Cursor',
			group: 'IDE / code editor',
			format: 'mcp-servers-json',
			paths: [
				'Per-project: .cursor/mcp.json',
				'Global: ~/.cursor/mcp.json',
			],
		},
		{
			id: 'windsurf',
			name: 'Windsurf',
			group: 'IDE / code editor',
			format: 'mcp-servers-json',
			paths: ['~/.codeium/windsurf/mcp_config.json'],
		},
		{
			id: 'vscode',
			name: 'VS Code (native MCP / Copilot)',
			group: 'IDE / code editor',
			format: 'vscode-servers-json',
			paths: [
				'Per-project: .vscode/mcp.json',
				'Global: settings.json → "mcp": { "servers": { … } }',
			],
			note: 'VS Code uses `servers` (not `mcpServers`) and wants an explicit `"type": "stdio"`.',
		},
		{
			id: 'cline',
			name: 'Cline (VS Code)',
			group: 'IDE / code editor',
			format: 'mcp-servers-json',
			paths: ['VS Code → Cline → MCP Servers → Configure (cline_mcp_settings.json)'],
		},
		{
			id: 'roo-code',
			name: 'Roo Code (VS Code)',
			group: 'IDE / code editor',
			format: 'mcp-servers-json',
			paths: ['VS Code → Roo Code → MCP Servers → Edit Global / Project (same shape as Cline)'],
		},
		{
			id: 'continue',
			name: 'Continue.dev (VS Code / JetBrains)',
			group: 'IDE / code editor',
			format: 'continue-experimental-json',
			paths: [
				'~/.continue/config.json (experimental.modelContextProtocolServers)',
				'Or per-project .continue/config.json',
			],
			note: 'Continue nests MCP servers under experimental.modelContextProtocolServers as an array.',
		},
		{
			id: 'zed',
			name: 'Zed',
			group: 'IDE / code editor',
			format: 'zed-context-json',
			paths: [
				'~/.config/zed/settings.json → "context_servers": { … }',
			],
			note: 'Zed calls them "context servers" but the stdio spec is the same.',
		},

		// ── CLI / terminal ──────────────────────────────────────
		{
			id: 'codex',
			name: 'Codex CLI (OpenAI)',
			group: 'CLI / terminal',
			format: 'codex-toml',
			paths: ['~/.codex/config.toml'],
			note: 'Codex uses TOML, not JSON. Append the snippet below under the existing [mcp_servers.*] table.',
		},
		{
			id: 'gemini-cli',
			name: 'Gemini CLI',
			group: 'CLI / terminal',
			format: 'mcp-servers-json',
			paths: [
				'Global: ~/.gemini/settings.json',
				'Per-project: .gemini/settings.json',
			],
		},
		{
			id: 'warp',
			name: 'Warp',
			group: 'CLI / terminal',
			format: 'mcp-servers-json',
			paths: ['Warp Settings → AI → MCP Servers → Add (stored in ~/.warp/mcp_servers.json)'],
		},

		// ── Other ───────────────────────────────────────────────
		{
			id: 'other',
			name: 'Other / custom',
			group: 'Other',
			format: 'mcp-servers-json',
			paths: ['Use the command + args + env below with whatever your MCP client expects.'],
		},
	];
	let selectedClientId = 'claude-desktop';
	$: selectedClient = MCP_CLIENTS.find((c) => c.id === selectedClientId) || MCP_CLIENTS[0];
	$: clientGroups = Array.from(new Set(MCP_CLIENTS.map((c) => c.group)));

	let refreshTimer: ReturnType<typeof setInterval> | null = null;

	// ── Lifecycle ────────────────────────────────────────────────────────
	onMount(async () => {
		try {
			activeSessionId = localStorage.getItem(SESSION_STORAGE_KEY) || '';
		} catch {}

		await refreshAll();
		loading = false;

		refreshTimer = setInterval(refreshAll, REFRESH_MS);
	});

	onDestroy(() => {
		if (refreshTimer) clearInterval(refreshTimer);
	});

	async function refreshAll() {
		await Promise.all([loadContext(), loadSessions(), loadActivity(), pingBackend()]);
		if (activeSessionId) await loadActiveDetail();
	}

	async function pingBackend() {
		try {
			await checkHealth();
			backendOk = true;
		} catch {
			backendOk = false;
		}
	}

	async function loadContext() {
		if (context) return; // static
		try {
			context = await getAiDropzoneContext();
		} catch (e) {
			// non-fatal — config card hides
		}
	}

	async function loadSessions() {
		try {
			const r = await listAiDropzoneSessions({ limit: 40, includeClosed: true });
			sessions = r.sessions || [];
			if (activeSessionId && !sessions.find((s) => s.id === activeSessionId)) {
				activeSessionId = '';
				persistActive('');
			}
		} catch (e) {
			error = `Failed to load sessions: ${(e as Error).message}`;
		}
	}

	async function loadActiveDetail() {
		if (!activeSessionId) {
			activeDetail = null;
			return;
		}
		try {
			activeDetail = await getAiDropzoneSession(activeSessionId);
		} catch (e) {
			activeDetail = null;
		}
	}

	async function loadActivity() {
		try {
			const [intake, runs] = await Promise.all([
				getRecentIntake(12).catch(() => null),
				getBacktestingRuns(12).catch(() => ({ runs: [] })),
			]);
			recentIntake = intake;
			recentRuns = runs.runs || [];
		} catch {}
	}

	// ── Session actions ─────────────────────────────────────────────────
	function persistActive(id: string) {
		try {
			if (id) localStorage.setItem(SESSION_STORAGE_KEY, id);
			else localStorage.removeItem(SESSION_STORAGE_KEY);
		} catch {}
	}

	async function selectSession(id: string) {
		activeSessionId = id;
		persistActive(id);
		await loadActiveDetail();
	}

	async function createSession() {
		if (sessionBusy) return;
		sessionBusy = true;
		error = null;
		success = null;
		try {
			const s = await createAiDropzoneSession({
				label: newSessionLabel.trim(),
				objective: newSessionObjective.trim(),
				actor: 'ui',
			});
			newSessionLabel = '';
			newSessionObjective = '';
			showNewSessionForm = false;
			await loadSessions();
			await selectSession(s.id);
			success = `Session ${s.id} opened`;
		} catch (e) {
			error = `Create failed: ${(e as Error).message}`;
		} finally {
			sessionBusy = false;
		}
	}

	async function closeActiveSession() {
		if (!activeSessionId || sessionBusy) return;
		if (!confirm(`Close session ${activeSessionId}?`)) return;
		sessionBusy = true;
		try {
			await closeAiDropzoneSession(activeSessionId);
			await loadSessions();
			await loadActiveDetail();
			success = `Session ${activeSessionId} closed`;
		} catch (e) {
			error = `Close failed: ${(e as Error).message}`;
		} finally {
			sessionBusy = false;
		}
	}

	// ── Manual register / backtest ───────────────────────────────────────
	async function runScan() {
		const path = registerFilePath.trim();
		if (!path) return;
		scanBusy = true;
		scanError = null;
		scanReport = null;
		try {
			scanReport = await scanStrategy(path);
			scannedPath = path;
		} catch (e) {
			scanError = `Scan failed: ${(e as Error).message}`;
		} finally {
			scanBusy = false;
		}
	}

	async function submitRegister() {
		const path = registerFilePath.trim();
		if (!path) return;
		// AST gate: refuse to register a path that hasn't been scanned clean
		// in this session. Forces operator awareness of forbidden imports
		// before code lands in forven/strategies/custom/.
		if (!scanReport || scannedPath !== path) {
			error = 'Run "Scan" first — AST scan is required before registering.';
			return;
		}
		if (!scanReport.ok) {
			error = `Blocked by AST scan: ${scanReport.findings.length} finding(s). Fix the strategy file before registering.`;
			return;
		}
		registerBusy = true;
		error = null;
		success = null;
		try {
			const r = await registerStrategyFile({
				file_path: path,
				session_id: activeSessionId || null,
			});
			registerFilePath = '';
			scanReport = null;
			scannedPath = '';
			success = `Registered ${r.strategy_id || r.module_name} (${r.stage})`;
			await loadActivity();
			if (activeSessionId) await loadActiveDetail();
		} catch (e) {
			error = `Register failed: ${(e as Error).message}`;
		} finally {
			registerBusy = false;
		}
	}

	async function submitBacktest() {
		const sid = backtestStrategyId.trim();
		const did = backtestDatasetId.trim();
		if (!sid || !did) return;
		backtestBusy = true;
		error = null;
		success = null;
		try {
			const body: Record<string, unknown> = { strategy_id: sid, dataset_id: did };
			if (activeSessionId) body.session_id = activeSessionId;
			const r = await fetchApi<Record<string, unknown>>('/backtesting/run', {
				method: 'POST',
				body: JSON.stringify(body),
			});
			success = `Run submitted: ${r.result_id || r.job_id || 'ok'}`;
			await loadActivity();
			if (activeSessionId) await loadActiveDetail();
		} catch (e) {
			error = `Gauntlet failed: ${(e as Error).message}`;
		} finally {
			backtestBusy = false;
		}
	}

	// ── MCP config ───────────────────────────────────────────────────────
	function backendUrl(): string {
		const origin = typeof window !== 'undefined' ? window.location.origin : 'http://127.0.0.1:8003';
		return origin.replace(/:\d+$/, ':8003');
	}

	function serverSpec() {
		return {
			command: 'python',
			args: ['-m', 'forven.mcp_server'],
			env: {
				FORVEN_API_URL: backendUrl(),
				FORVEN_API_KEY: 'your-api-key',
				FORVEN_OPERATOR_KEY: 'your-operator-key',
			},
		};
	}

	function buildMcpConfig(format: ConfigFormat = selectedClient.format): string {
		const spec = serverSpec();
		switch (format) {
			case 'vscode-servers-json': {
				// VS Code native MCP: { "servers": { name: { type: "stdio", command, args, env } } }
				const cfg = {
					servers: {
						forven: {
							type: 'stdio',
							command: spec.command,
							args: spec.args,
							env: spec.env,
						},
					},
				};
				return JSON.stringify(cfg, null, 2);
			}
			case 'zed-context-json': {
				// Zed: context_servers: { name: { command: { path, args, env } } }
				const cfg = {
					context_servers: {
						forven: {
							command: {
								path: spec.command,
								args: spec.args,
								env: spec.env,
							},
						},
					},
				};
				return JSON.stringify(cfg, null, 2);
			}
			case 'continue-experimental-json': {
				// Continue.dev: experimental.modelContextProtocolServers: [{ transport: {...} }]
				const cfg = {
					experimental: {
						modelContextProtocolServers: [
							{
								transport: {
									type: 'stdio',
									command: spec.command,
									args: spec.args,
									env: spec.env,
								},
							},
						],
					},
				};
				return JSON.stringify(cfg, null, 2);
			}
			case 'codex-toml': {
				// Codex CLI TOML
				const envLines = Object.entries(spec.env)
					.map(([k, v]) => `${k} = "${v}"`)
					.join(', ');
				return [
					'[mcp_servers.forven]',
					`command = "${spec.command}"`,
					`args = [${spec.args.map((a) => `"${a}"`).join(', ')}]`,
					`env = { ${envLines} }`,
				].join('\n');
			}
			case 'mcp-servers-json':
			default: {
				const cfg = { mcpServers: { forven: serverSpec() } };
				return JSON.stringify(cfg, null, 2);
			}
		}
	}

	$: configLanguageLabel = selectedClient.format === 'codex-toml' ? 'TOML' : 'JSON';

	function buildCliCommand(): string {
		// For clients that accept CLI registration (e.g. `claude mcp add`).
		return (
			`claude mcp add forven ` +
			`-e FORVEN_API_URL=${backendUrl()} ` +
			`-e FORVEN_API_KEY=your-api-key ` +
			`-e FORVEN_OPERATOR_KEY=your-operator-key ` +
			`-- python -m forven.mcp_server`
		);
	}

	async function copyConfig() {
		try {
			await navigator.clipboard.writeText(buildMcpConfig());
			success = 'Config copied to clipboard';
		} catch {
			error = 'Clipboard access denied';
		}
	}

	async function copyCli() {
		try {
			await navigator.clipboard.writeText(buildCliCommand());
			success = 'Command copied to clipboard';
		} catch {
			error = 'Clipboard access denied';
		}
	}

	// ── HTTP harness (no-MCP) snippets ───────────────────────────────────
	// For harnesses where MCP isn't available (the Tauri app, Codex, sidecars,
	// CI) — the same toolset over the REST API via the zero-dependency
	// `forven.agent` client/CLI, or any HTTP client.
	function httpCliSnippet(): string {
		const base = backendUrl();
		return [
			'# Any shell — Claude Code, Codex, CI. JSON in / JSON out.',
			'# (set FORVEN_API_URL if the backend is not on this machine)',
			`export FORVEN_API_URL=${base}`,
			'python -m forven.agent health            # or: forven-agent health',
			'python -m forven.agent list --status paper',
			'python -m forven.agent backtest --strategy S00719 --dataset BTC/USDT-1h --compact',
			'# write a strategy .py to forven/strategies/custom/, then run the full loop:',
			'python -m forven.agent enqueue --file /abs/path/strategy.py --dataset BTC/USDT-1h',
			'python -m forven.agent wait-paper --strategies S00719 --timeout 1800',
		].join('\n');
	}
	function httpPySnippet(): string {
		return [
			'from forven.agent import ForvenAgentClient',
			'fc = ForvenAgentClient()   # FORVEN_API_URL / base_url override',
			'fc.health()',
			'# register -> 365d backtest -> quick-screen -> enqueue to gauntlet (force=false)',
			'verdict = fc.enqueue_candidate("/abs/path/strategy.py", "BTC/USDT-1h")',
		].join('\n');
	}
	function httpTsSnippet(): string {
		return [
			"// In-app / Tauri / browser — reuses the app's fetchApi (auth + base discovery)",
			"import ForvenAgent from '$lib/api/agent';",
			'const v = await ForvenAgent.enqueueCandidate("/abs/path/strategy.py", "BTC/USDT-1h");',
		].join('\n');
	}
	const HTTP_ENDPOINTS: { method: string; path: string; purpose: string }[] = [
		{ method: 'GET', path: '/api/health', purpose: 'liveness' },
		{ method: 'GET', path: '/api/ai-dropzone/context', purpose: 'datasets, template, param families' },
		{ method: 'GET', path: '/api/strategies?status=', purpose: 'list strategies' },
		{ method: 'POST', path: '/api/strategies/intake/register-file', purpose: 'register a .py' },
		{ method: 'POST', path: '/api/backtesting/run', purpose: 'backtest (IS + OOS)' },
		{ method: 'POST', path: '/api/backtesting/optimize', purpose: 'parameter search' },
		{ method: 'POST', path: '/api/robustness/walk-forward/submit', purpose: 'persisted walk-forward (paper-gate evidence)' },
		{ method: 'POST', path: '/api/robustness/cost-stress/submit', purpose: 'persisted cost stress' },
		{ method: 'POST', path: '/api/robustness/param-jitter/submit', purpose: 'persisted param jitter' },
		{ method: 'GET', path: '/api/robustness/results/{id}', purpose: 'poll a robustness run' },
		{ method: 'GET', path: '/api/lifecycle/strategies/{id}/readiness', purpose: 'promotion checklist' },
		{ method: 'POST', path: '/api/strategies/{id}/promote', purpose: 'advance lifecycle stage' },
	];

	async function copyText(text: string, label: string) {
		try {
			await navigator.clipboard.writeText(text);
			success = `${label} copied to clipboard`;
		} catch {
			error = 'Clipboard access denied';
		}
	}

	// ── Formatting ───────────────────────────────────────────────────────
	function fmtTime(iso?: string | null): string {
		if (!iso) return '';
		try {
			const d = new Date(iso);
			return d.toLocaleString(undefined, { month: 'short', day: '2-digit', hour: '2-digit', minute: '2-digit' });
		} catch {
			return iso;
		}
	}

	function fmtAgo(iso?: string | null): string {
		if (!iso) return '';
		const ms = Date.now() - new Date(iso).getTime();
		if (!isFinite(ms) || ms < 0) return '';
		const m = Math.floor(ms / 60000);
		if (m < 1) return 'just now';
		if (m < 60) return `${m}m ago`;
		const h = Math.floor(m / 60);
		if (h < 48) return `${h}h ago`;
		return `${Math.floor(h / 24)}d ago`;
	}

	function fmtMetric(row: BacktestingRunSummary): string {
		const m = row.metrics?.out_of_sample as Record<string, unknown> | undefined;
		if (!m) return '—';
		const r = Number(m.total_return_pct ?? m.total_return ?? 0);
		const s = Number(m.sharpe ?? 0);
		return `ret ${r.toFixed(1)}% · sh ${s.toFixed(2)}`;
	}

	$: openSessions = sessions.filter((s) => s.status !== 'closed');
	$: closedSessions = sessions.filter((s) => s.status === 'closed');
</script>

<div class="h-full overflow-y-auto bg-black text-white font-mono">
	<!-- Header ─────────────────────────────────────────────────────────── -->
	<div class="border-b border-[#222] px-4 py-3 sticky top-0 bg-black z-10">
		<div class="flex items-center justify-between gap-4 flex-wrap">
			<div class="flex items-center gap-4">
				<div>
					<h1 class="text-sm tracking-widest uppercase text-gray-200">AI Drop Zone</h1>
					<p class="text-[10px] text-gray-500 mt-0.5">MCP &amp; HTTP cockpit — watch any AI client drive the lab</p>
				</div>
				<div class="flex items-center gap-3 ml-4">
					<div class="flex items-center gap-1.5">
						<div class="w-1.5 h-1.5 rounded-full {backendOk ? 'bg-emerald-400' : 'bg-red-400'}"></div>
						<span class="text-[10px] text-gray-500">Backend</span>
						<span class="help-tip">?<span class="help-text">Forven HTTP API at /api. If this is red, MCP tool calls will also fail — MCP proxies through this API.</span></span>
					</div>
					<div class="text-[10px] text-gray-500">
						<span class="text-gray-400">{openSessions.length}</span> open /
						<span class="text-gray-400">{sessions.length}</span> total
						<span class="help-tip">?<span class="help-text">Sessions auto-open when an MCP client starts work, tag everything it does, and close on disconnect — or after ~6h idle (server sweep). Open = genuinely in use.</span></span>
					</div>
				</div>
			</div>

			<!-- Session picker -->
			<div class="flex items-center gap-2 flex-wrap">
				<label for="adz-session-select" class="text-[10px] text-gray-500 uppercase tracking-widest">Session</label>
				<select
					id="adz-session-select"
					class="bg-[#111] border border-[#333] text-gray-200 text-xs px-2 py-1 rounded min-w-[220px]"
					value={activeSessionId}
					on:change={(e) => selectSession((e.currentTarget as HTMLSelectElement).value)}
				>
					<option value="">— none —</option>
					{#if openSessions.length > 0}
						<optgroup label="Open">
							{#each openSessions as s}
								<option value={s.id}>{s.id}{s.label ? ` · ${s.label}` : ''}</option>
							{/each}
						</optgroup>
					{/if}
					{#if closedSessions.length > 0}
						<optgroup label="Closed">
							{#each closedSessions as s}
								<option value={s.id}>{s.id}{s.label ? ` · ${s.label}` : ''} (closed)</option>
							{/each}
						</optgroup>
					{/if}
				</select>
				<button
					class="text-[10px] uppercase tracking-widest px-2 py-1 border border-[#333] hover:border-emerald-500 hover:text-emerald-400 rounded"
					on:click={() => (showNewSessionForm = !showNewSessionForm)}
				>+ New</button>
				{#if activeSessionId && activeDetail?.status !== 'closed'}
					<button
						class="text-[10px] uppercase tracking-widest px-2 py-1 border border-[#333] hover:border-red-500 hover:text-red-400 rounded"
						on:click={closeActiveSession}
						disabled={sessionBusy}
					>Close</button>
				{/if}
			</div>
		</div>

		{#if showNewSessionForm}
			<div class="mt-2 flex items-center gap-2 flex-wrap">
				<input
					class="bg-[#111] border border-[#333] text-gray-200 text-xs px-2 py-1 rounded flex-1 min-w-[200px]"
					placeholder="Label (optional)"
					bind:value={newSessionLabel}
				/>
				<input
					class="bg-[#111] border border-[#333] text-gray-200 text-xs px-2 py-1 rounded flex-1 min-w-[240px]"
					placeholder="Objective (optional)"
					bind:value={newSessionObjective}
				/>
				<button
					class="text-[10px] uppercase tracking-widest px-3 py-1 bg-emerald-600 hover:bg-emerald-500 text-black font-semibold rounded"
					on:click={createSession}
					disabled={sessionBusy}
				>Create</button>
				<button
					class="text-[10px] uppercase tracking-widest px-2 py-1 border border-[#333] hover:border-gray-500 text-gray-400 rounded"
					on:click={() => (showNewSessionForm = false)}
				>Cancel</button>
			</div>
		{/if}
	</div>

	{#if error}
		<div class="mx-4 mt-3 border border-red-900 bg-red-950/40 text-red-300 text-xs px-3 py-2 rounded flex items-center justify-between">
			<span>{error}</span>
			<button class="text-red-500 hover:text-red-300" on:click={() => (error = null)}>×</button>
		</div>
	{/if}
	{#if success}
		<div class="mx-4 mt-3 border border-emerald-900 bg-emerald-950/40 text-emerald-300 text-xs px-3 py-2 rounded flex items-center justify-between">
			<span>{success}</span>
			<button class="text-emerald-500 hover:text-emerald-300" on:click={() => (success = null)}>×</button>
		</div>
	{/if}

	{#if loading}
		<div class="p-6 text-center text-gray-500 text-xs">Loading…</div>
	{:else}
		<!-- MCP connect card ─────────────────────────────────────────── -->
		<div class="mx-4 mt-4 border border-[#222] rounded bg-[#0a0a0a]">
			<button
				class="w-full px-4 py-3 flex items-center justify-between hover:bg-[#111]"
				on:click={() => (showMcpConfig = !showMcpConfig)}
			>
				<div class="flex items-center gap-3">
					<div class="w-1.5 h-1.5 rounded-full {backendOk ? 'bg-emerald-400' : 'bg-gray-600'}"></div>
					<span class="text-xs uppercase tracking-widest text-gray-300">Connect an MCP client</span>
					<span class="text-[10px] text-gray-600">{MCP_TOOL_COUNT} MCP tools · the full loop: register → backtest → optimize → robustness → promote</span>
				</div>
				<span class="text-gray-600 text-xs">{showMcpConfig ? '▼' : '▶'}</span>
			</button>
			{#if showMcpConfig}
				<div class="px-4 pb-4 border-t border-[#222] pt-3">
					<!-- Client picker -->
					<div class="flex items-center gap-2 flex-wrap mb-3">
						<label for="adz-mcp-client" class="text-[10px] uppercase tracking-widest text-gray-500">Client</label>
						<select
							id="adz-mcp-client"
							class="bg-[#111] border border-[#333] text-gray-200 text-xs px-2 py-1 rounded min-w-[220px]"
							bind:value={selectedClientId}
						>
							{#each clientGroups as grp}
								<optgroup label={grp}>
									{#each MCP_CLIENTS.filter((c) => c.group === grp) as c}
										<option value={c.id}>{c.name}</option>
									{/each}
								</optgroup>
							{/each}
						</select>
						<span class="text-[10px] text-gray-600">
							{#if selectedClient.format === 'mcp-servers-json'}
								Standard <code class="bg-[#111] px-1 rounded">mcpServers</code> JSON.
							{:else if selectedClient.format === 'vscode-servers-json'}
								VS Code native MCP format (<code class="bg-[#111] px-1 rounded">servers</code>).
							{:else if selectedClient.format === 'zed-context-json'}
								Zed <code class="bg-[#111] px-1 rounded">context_servers</code> format.
							{:else if selectedClient.format === 'continue-experimental-json'}
								Continue.dev <code class="bg-[#111] px-1 rounded">experimental</code> format.
							{:else if selectedClient.format === 'codex-toml'}
								Codex CLI TOML format.
							{/if}
						</span>
					</div>

					<!-- Per-client location hint -->
					<div class="border border-[#1a1a1a] rounded bg-[#050505] px-3 py-2 mb-3">
						<div class="text-[10px] uppercase tracking-widest text-gray-500 mb-1">Where to paste</div>
						<ul class="text-[11px] text-gray-300 space-y-0.5">
							{#each selectedClient.paths as p}
								<li><code class="bg-[#111] px-1.5 py-0.5 rounded text-gray-300">{p}</code></li>
							{/each}
						</ul>
						{#if selectedClient.note}
							<div class="text-[10px] text-gray-500 mt-1.5">{selectedClient.note}</div>
						{/if}
					</div>

					<!-- Config snippet -->
					<p class="text-[11px] text-gray-400 mb-1.5">
						Merge this into the target file. Replace the
						<code class="bg-[#111] px-1.5 py-0.5 rounded text-gray-300">your-*-key</code>
						placeholders if your backend requires auth — remove those env keys if it doesn't.
					</p>
					<pre class="bg-[#050505] border border-[#222] rounded p-3 text-[11px] text-gray-300 overflow-x-auto">{buildMcpConfig()}</pre>
					<div class="flex items-center gap-2 mt-2 flex-wrap">
						<button
							class="text-[10px] uppercase tracking-widest px-3 py-1 border border-[#333] hover:border-emerald-500 hover:text-emerald-400 rounded"
							on:click={copyConfig}
						>Copy {configLanguageLabel}</button>
						{#if selectedClientId === 'claude-code'}
							<button
								class="text-[10px] uppercase tracking-widest px-3 py-1 border border-[#333] hover:border-emerald-500 hover:text-emerald-400 rounded"
								on:click={copyCli}
								title={buildCliCommand()}
							>Copy CLI command</button>
						{/if}
						<a
							href="/docs/mcp-server.md"
							class="text-[10px] uppercase tracking-widest px-3 py-1 border border-[#333] hover:border-gray-500 text-gray-400 rounded"
						>Full guide</a>
					</div>
				</div>
			{/if}
		</div>

		<!-- HTTP harness connect card (no MCP) ──────────────────────── -->
		<div class="mx-4 mt-4 border border-[#222] rounded bg-[#0a0a0a]">
			<button
				class="w-full px-4 py-3 flex items-center justify-between hover:bg-[#111]"
				on:click={() => (showHttpConfig = !showHttpConfig)}
			>
				<div class="flex items-center gap-3">
					<div class="w-1.5 h-1.5 rounded-full {backendOk ? 'bg-emerald-400' : 'bg-gray-600'}"></div>
					<span class="text-xs uppercase tracking-widest text-gray-300">Connect over HTTP (no MCP)</span>
					<span class="text-[10px] text-gray-600">Same tools via the REST API · for the Tauri app, Codex, sidecars &amp; CI</span>
				</div>
				<span class="text-gray-600 text-xs">{showHttpConfig ? '▼' : '▶'}</span>
			</button>
			{#if showHttpConfig}
				<div class="px-4 pb-4 border-t border-[#222] pt-3 space-y-3">
					<p class="text-[11px] text-gray-400">
						The MCP server is just a thin wrapper over this REST API — use HTTP directly when MCP
						isn't available (the in-app/Tauri assistant, Codex, sidecars, CI). The
						<code class="bg-[#111] px-1 rounded">forven.agent</code> client is stdlib-only (zero deps).
						Base URL:
						<code class="bg-[#111] px-1.5 py-0.5 rounded text-gray-300">{backendUrl()}</code>
						<button
							class="ml-1 text-[10px] uppercase tracking-widest px-2 py-0.5 border border-[#333] hover:border-emerald-500 hover:text-emerald-400 rounded"
							on:click={() => copyText(backendUrl(), 'Base URL')}
						>Copy</button>
					</p>

					<!-- CLI -->
					<div>
						<div class="text-[10px] uppercase tracking-widest text-gray-500 mb-1">CLI — Claude Code / Codex / any shell</div>
						<pre class="bg-[#050505] border border-[#222] rounded p-3 text-[11px] text-gray-300 overflow-x-auto">{httpCliSnippet()}</pre>
						<button
							class="mt-1.5 text-[10px] uppercase tracking-widest px-3 py-1 border border-[#333] hover:border-emerald-500 hover:text-emerald-400 rounded"
							on:click={() => copyText(httpCliSnippet(), 'CLI commands')}
						>Copy CLI</button>
					</div>

					<!-- Python + TypeScript -->
					<div class="grid grid-cols-1 lg:grid-cols-2 gap-3">
						<div>
							<div class="text-[10px] uppercase tracking-widest text-gray-500 mb-1">Python — sidecar / script</div>
							<pre class="bg-[#050505] border border-[#222] rounded p-3 text-[11px] text-gray-300 overflow-x-auto">{httpPySnippet()}</pre>
							<button
								class="mt-1.5 text-[10px] uppercase tracking-widest px-3 py-1 border border-[#333] hover:border-emerald-500 hover:text-emerald-400 rounded"
								on:click={() => copyText(httpPySnippet(), 'Python snippet')}
							>Copy</button>
						</div>
						<div>
							<div class="text-[10px] uppercase tracking-widest text-gray-500 mb-1">TypeScript — in-app / Tauri</div>
							<pre class="bg-[#050505] border border-[#222] rounded p-3 text-[11px] text-gray-300 overflow-x-auto">{httpTsSnippet()}</pre>
							<button
								class="mt-1.5 text-[10px] uppercase tracking-widest px-3 py-1 border border-[#333] hover:border-emerald-500 hover:text-emerald-400 rounded"
								on:click={() => copyText(httpTsSnippet(), 'TypeScript snippet')}
							>Copy</button>
						</div>
					</div>

					<!-- Endpoint reference -->
					<div>
						<div class="text-[10px] uppercase tracking-widest text-gray-500 mb-1">Endpoints (any language)</div>
						<div class="border border-[#1a1a1a] rounded bg-[#050505] overflow-hidden">
							{#each HTTP_ENDPOINTS as ep}
								<div class="flex items-center gap-3 px-3 py-1.5 border-b border-[#141414] last:border-b-0 text-[11px]">
									<span class="w-12 shrink-0 font-semibold {ep.method === 'GET' ? 'text-emerald-400' : 'text-yellow-400'}">{ep.method}</span>
									<code class="text-gray-300">{ep.path}</code>
									<span class="ml-auto text-[10px] text-gray-600">{ep.purpose}</span>
								</div>
							{/each}
						</div>
					</div>

					<p class="text-[10px] text-gray-500">
						Auth is only needed if the backend is exposed beyond localhost: send
						<code class="bg-[#111] px-1 rounded">x-api-key</code> /
						<code class="bg-[#111] px-1 rounded">x-operator-key</code> headers, or set
						<code class="bg-[#111] px-1 rounded">FORVEN_API_KEY</code> /
						<code class="bg-[#111] px-1 rounded">FORVEN_OPERATOR_KEY</code>. Full reference:
						<code class="bg-[#111] px-1 rounded">forven/agent/README.md</code>.
					</p>
				</div>
			{/if}
		</div>

		<!-- Main grid ─────────────────────────────────────────────────── -->
		<div class="mx-4 mt-4 grid grid-cols-1 lg:grid-cols-3 gap-4">
			<!-- Active session (2 cols) -->
			<div class="lg:col-span-2 border border-[#222] rounded bg-[#0a0a0a]">
				<div class="px-4 py-3 border-b border-[#222] flex items-center justify-between">
					<div>
						<h2 class="text-xs uppercase tracking-widest text-gray-300">Active Session</h2>
						{#if activeDetail}
							<p class="text-[10px] text-gray-500 mt-0.5">
								{activeDetail.id} · {activeDetail.label || 'unlabeled'} · {activeDetail.status}
								{#if activeDetail.last_activity_at}· <span class="text-gray-600">last activity {fmtAgo(activeDetail.last_activity_at)}</span>{/if}
								{#if activeDetail.objective}· <span class="text-gray-600">{activeDetail.objective}</span>{/if}
							</p>
						{:else}
							<p class="text-[10px] text-gray-500 mt-0.5">Pick a session above to watch it live</p>
						{/if}
					</div>
					{#if activeDetail}
						<div class="text-[10px] text-gray-500 flex gap-3">
							<span><span class="text-gray-300">{activeDetail.strategies.length}</span> strategies</span>
							<span><span class="text-gray-300">{activeDetail.runs.length}</span> runs</span>
						</div>
					{/if}
				</div>

				{#if !activeDetail}
					<div class="p-6 text-center text-xs text-gray-600">
						No session selected. Sessions open automatically when an MCP client starts working —
						pick one above, or tell your assistant: <em class="text-gray-400">"Open a Forven session for X"</em>
					</div>
				{:else}
					<!-- Strategies -->
					<div class="px-4 py-3">
						<div class="text-[10px] text-gray-500 uppercase tracking-widest mb-2">Strategies tagged to this session</div>
						{#if activeDetail.strategies.length === 0}
							<div class="text-[11px] text-gray-600 italic">None yet</div>
						{:else}
							<div class="space-y-1 max-h-56 overflow-y-auto">
								{#each activeDetail.strategies as s}
									<div class="text-[11px] flex items-center justify-between gap-3 border border-[#1a1a1a] rounded px-2 py-1.5 hover:border-[#333]">
										<div class="flex-1 min-w-0">
											<a href="/lab/strategy/{s.id}" class="text-gray-200 hover:text-emerald-400 font-semibold">{s.id}</a>
											<span class="text-gray-500 ml-2">{s.name || s.type}</span>
											<span class="text-gray-600 ml-2">{s.symbol} · {s.timeframe}</span>
										</div>
										<div class="text-gray-600">{s.stage}</div>
										<div class="text-gray-700 text-[10px] whitespace-nowrap">{fmtTime(s.created_at)}</div>
									</div>
								{/each}
							</div>
						{/if}
					</div>

					<!-- Runs -->
					<div class="px-4 py-3 border-t border-[#222]">
						<div class="text-[10px] text-gray-500 uppercase tracking-widest mb-2">Gauntlet runs in this session</div>
						{#if activeDetail.runs.length === 0}
							<div class="text-[11px] text-gray-600 italic">None yet</div>
						{:else}
							<div class="space-y-1 max-h-56 overflow-y-auto">
								{#each activeDetail.runs as r}
									<div class="text-[11px] flex items-center justify-between gap-3 border border-[#1a1a1a] rounded px-2 py-1.5 hover:border-[#333]">
										<div class="flex-1 min-w-0">
											<span class="text-gray-200 font-semibold">{r.result_id}</span>
											<span class="text-gray-600 ml-2">{r.strategy_id}</span>
										</div>
										<div class="text-gray-500">{r.symbol} · {r.timeframe}</div>
										<div class="text-gray-700 text-[10px] whitespace-nowrap">{fmtTime(r.created_at)}</div>
									</div>
								{/each}
							</div>
						{/if}
					</div>
				{/if}
			</div>

			<!-- Activity feed -->
			<div class="border border-[#222] rounded bg-[#0a0a0a]">
				<div class="px-4 py-3 border-b border-[#222]">
					<h2 class="text-xs uppercase tracking-widest text-gray-300">Recent Activity</h2>
					<p class="text-[10px] text-gray-500 mt-0.5">Across all sessions · refreshes every {REFRESH_MS / 1000}s</p>
				</div>
				<div class="px-4 py-3">
					<div class="text-[10px] text-gray-500 uppercase tracking-widest mb-2">Latest intake</div>
					{#if !recentIntake || recentIntake.strategies.length === 0}
						<div class="text-[11px] text-gray-600 italic">No recent intake</div>
					{:else}
						<div class="space-y-1 max-h-48 overflow-y-auto">
							{#each recentIntake.strategies.slice(0, 10) as s}
								<div class="text-[11px] flex items-center justify-between gap-2 border border-[#1a1a1a] rounded px-2 py-1 hover:border-[#333]">
									<a href="/lab/strategy/{s.id}" class="text-gray-300 hover:text-emerald-400 truncate">{s.id}</a>
									<span class="text-gray-600 text-[10px]">{s.stage}</span>
								</div>
							{/each}
						</div>
					{/if}
				</div>
				<div class="px-4 py-3 border-t border-[#222]">
					<div class="text-[10px] text-gray-500 uppercase tracking-widest mb-2">Latest runs</div>
					{#if recentRuns.length === 0}
						<div class="text-[11px] text-gray-600 italic">No recent runs</div>
					{:else}
						<div class="space-y-1 max-h-48 overflow-y-auto">
							{#each recentRuns.slice(0, 10) as r}
								<div class="text-[11px] border border-[#1a1a1a] rounded px-2 py-1 hover:border-[#333]">
									<div class="flex items-center justify-between gap-2">
										<span class="text-gray-300 truncate">{r.id || r.run_id}</span>
										<span class="text-gray-600 text-[10px]">{fmtTime(r.created_at)}</span>
									</div>
									<div class="text-gray-600 text-[10px] truncate">{r.strategy_id} · {fmtMetric(r)}</div>
								</div>
							{/each}
						</div>
					{/if}
				</div>
			</div>
		</div>

		<!-- Manual fallback ──────────────────────────────────────────── -->
		<div class="mx-4 mt-4 mb-8 border border-[#222] rounded bg-[#0a0a0a]">
			<div class="px-4 py-3 border-b border-[#222]">
				<h2 class="text-xs uppercase tracking-widest text-gray-300">Manual Fallback</h2>
				<p class="text-[10px] text-gray-500 mt-0.5">
					For when you're not driving via MCP. Tags to the active session if one is selected.
				</p>
			</div>
			<div class="grid grid-cols-1 md:grid-cols-2 gap-0">
				<!-- Register file -->
				<div class="px-4 py-3 border-b md:border-b-0 md:border-r border-[#222]">
					<div class="text-[10px] text-gray-500 uppercase tracking-widest mb-2">Register strategy file</div>
					<div class="flex gap-2 flex-wrap">
						<input
							class="flex-1 min-w-[220px] bg-[#111] border border-[#333] text-gray-200 text-xs px-2 py-1.5 rounded font-mono"
							placeholder="Absolute path e.g. C:\...\strategies\custom\foo.py"
							bind:value={registerFilePath}
							on:keydown={(e) => e.key === 'Enter' && (scanReport && scanClean && !scanStale ? submitRegister() : runScan())}
						/>
						<button
							class="text-[10px] uppercase tracking-widest px-3 py-1.5 border border-[#333] hover:border-amber-500 hover:text-amber-300 text-gray-300 rounded disabled:opacity-40"
							on:click={runScan}
							disabled={scanBusy || !registerFilePath.trim()}
							title="Static AST scan — checks for forbidden imports and dangerous calls"
						>{scanBusy ? '…' : 'Scan'}</button>
						<button
							class="terminal-button text-[10px] tracking-widest disabled:opacity-40"
							on:click={submitRegister}
							disabled={registerBusy || !registerFilePath.trim() || !scanClean || scanStale}
							title={!scanReport
								? 'Run "Scan" first'
								: scanStale
									? 'Path changed since last scan — re-scan'
									: !scanClean
										? 'Blocked: AST findings present'
										: 'Register strategy file'}
						>{registerBusy ? '…' : 'Register'}</button>
					</div>
					{#if context?.file_location}
						<div class="text-[10px] text-gray-600 mt-1.5">Workspace: {context.file_location}</div>
					{/if}

					{#if scanError}
						<div class="mt-2 border border-red-900 bg-red-950/40 text-red-300 text-[11px] px-2 py-1.5 rounded font-mono">
							{scanError}
						</div>
					{:else if scanReport && !scanStale}
						<div class="mt-2 border rounded px-2 py-1.5 {scanClean ? 'border-emerald-900 bg-emerald-950/20' : 'border-red-900 bg-red-950/20'}">
							<div class="text-[11px] {scanClean ? 'text-emerald-300' : 'text-red-300'}">
								{#if scanClean}
									✓ AST clean · {scanReport.line_count} lines · {scanReport.file_size_bytes} bytes
								{:else}
									✗ {scanReport.findings.length} AST finding(s) — register blocked
								{/if}
							</div>
							{#if !scanClean}
								<ul class="mt-1 space-y-0.5">
									{#each scanReport.findings.slice(0, 6) as f}
										<li class="text-[10px] text-gray-300 font-mono">
											<span class="text-red-400">{f.kind}</span>
											<span class="text-gray-500">L{f.lineno}:{f.col}</span>
											<span class="text-gray-300">— {f.message}</span>
										</li>
									{/each}
									{#if scanReport.findings.length > 6}
										<li class="text-[10px] text-gray-500">… {scanReport.findings.length - 6} more</li>
									{/if}
								</ul>
							{/if}
						</div>
					{:else if scanStale}
						<div class="mt-2 text-[10px] text-amber-400">Path changed since last scan — re-scan before registering.</div>
					{:else}
						<div class="mt-2 text-[10px] text-gray-500">Scan required: AI-generated strategies must pass the AST gate before registering.</div>
					{/if}
				</div>

				<!-- Run backtest -->
				<div class="px-4 py-3">
					<div class="text-[10px] text-gray-500 uppercase tracking-widest mb-2">Run backtest</div>
					<div class="flex gap-2 flex-wrap">
						<input
							class="flex-1 min-w-[140px] bg-[#111] border border-[#333] text-gray-200 text-xs px-2 py-1.5 rounded font-mono"
							placeholder="strategy_id (e.g. S00584)"
							bind:value={backtestStrategyId}
						/>
						<input
							class="flex-1 min-w-[120px] bg-[#111] border border-[#333] text-gray-200 text-xs px-2 py-1.5 rounded font-mono"
							placeholder="BTC/USDT-1h"
							bind:value={backtestDatasetId}
						/>
						<button
							class="terminal-button text-[10px] tracking-widest disabled:opacity-40"
							on:click={submitBacktest}
							disabled={backtestBusy || !backtestStrategyId.trim() || !backtestDatasetId.trim()}
						>{backtestBusy ? '…' : 'Run'}</button>
					</div>
				</div>
			</div>
		</div>
	{/if}
</div>

<style>
	.help-tip {
		position: relative;
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: 14px;
		height: 14px;
		border-radius: 9999px;
		border: 1px solid #333;
		background: #111;
		color: #666;
		font-size: 9px;
		font-weight: 600;
		cursor: help;
		flex-shrink: 0;
		margin-left: 4px;
		vertical-align: middle;
		line-height: 1;
	}
	.help-tip:hover {
		border-color: #555;
		color: #999;
		background: #1a1a1a;
	}
	.help-tip .help-text {
		display: none;
		position: absolute;
		bottom: calc(100% + 6px);
		left: 50%;
		transform: translateX(-50%);
		background: #1a1a1a;
		border: 1px solid #333;
		color: #ccc;
		font-size: 10px;
		font-weight: 400;
		padding: 6px 8px;
		border-radius: 4px;
		width: max-content;
		max-width: 260px;
		white-space: normal;
		line-height: 1.4;
		z-index: 50;
		text-transform: none;
		letter-spacing: normal;
		pointer-events: none;
	}
	.help-tip .help-text::after {
		content: '';
		position: absolute;
		top: 100%;
		left: 50%;
		transform: translateX(-50%);
		border: 4px solid transparent;
		border-top-color: #333;
	}
	.help-tip:hover .help-text {
		display: block;
	}
</style>
