<script lang="ts">
	import '../app.css';
	import { page } from '$app/stores';
	import { onMount, onDestroy } from 'svelte';
	import { get } from 'svelte/store';
	import { checkHealth, getSettings } from '$lib/api';
	import { backendConnected } from '$lib/stores';
	import { bootstrapActiveProcesses } from '$lib/stores/processTracker';
	import { startHeartbeat, stopHeartbeat } from '$lib/stores/heartbeat';
	import { connectForvenWs, disconnectForvenWs, forvenWsConnected } from '$lib/stores/forvenWebSocket';
	import { shouldMarkBackendDisconnected } from '$lib/utils/connectionHealth';
	import Sidebar from '$lib/components/Sidebar.svelte';
	import Toast from '$lib/components/Toast.svelte';
	import GlobalControlStrip from '$lib/components/forven/GlobalControlStrip.svelte';
	import LaunchBanner from '$lib/components/LaunchBanner.svelte';
	import RiskDisclaimerBanner from '$lib/components/RiskDisclaimerBanner.svelte';
	import AgentProviderBanner from '$lib/components/AgentProviderBanner.svelte';
	import UpdateBanner from '$lib/components/UpdateBanner.svelte';
	import PositionAlertWidget from '$lib/components/PositionAlertWidget.svelte';
	import AIChatPanel from '$lib/components/AIChatPanel.svelte';
	import { chatUnreadCount } from '$lib/stores/chatStore';
	import { assistantUI, toggleAssistant } from '$lib/stores/assistantUI';
	import { setRoute } from '$lib/stores/pageContext';
	import SetupWizardModal from '$lib/components/wizard/SetupWizardModal.svelte';
	import { wizardOpen, openWizard } from '$lib/stores/setupWizard';
	import SettingsSaveBar from '$lib/components/settings/shell/SettingsSaveBar.svelte';
	import { dirtyFields, originalValues, pendingValues } from '$lib/settings/dirty';

	let connectionStatus = 'checking';
	let pollersActive = false;
	let wsChannelActive = false;
	let processesBootstrapped = false;
	let wizardSettings: Record<string, unknown> | null = null;
	let prevDirtyCount = 0;

	async function reloadWizardSettings(): Promise<void> {
		try {
			const s = await getSettings();
			wizardSettings = s as unknown as Record<string, unknown>;
		} catch {
			// Swallow: wizard falls back to the prior snapshot; next /settings
			// fetch (e.g. on a later save or reopen) will pick up changes.
		}
	}

	// When the save bar transitions from dirty to clean, the backend now holds
	// newly-saved values. Refetch so satisfaction (e.g. has_credentials) flips
	// without requiring a reload.
	$: {
		const count = $dirtyFields.size;
		if (prevDirtyCount > 0 && count === 0) {
			void reloadWizardSettings();
		}
		prevDirtyCount = count;
	}

	$: saveBarValues = { ...$originalValues, ...$pendingValues };

	const TITLE_OVERRIDES: Record<string, string> = {
		'/': 'Dashboard',
		'/data': 'Data',
		'/all-trades': 'All Trades',
		'/trading': 'Trades',
		'/risk': 'Risk',
		'/lab': 'The Forge',
		'/hypotheses': 'Crucibles',
		'/agents': 'Agents',
		'/memory': 'Memory',
		'/tasks': 'Tasks',
		'/approval': 'Approvals',
		'/diagnostics': 'Diagnostics',
		'/integrations': 'Integrations',
		'/integrations/mcp': 'Integrations',
		'/settings': 'Settings',
	};

	const DESCRIPTION_OVERRIDES: Record<string, string> = {
		'/': 'Live trading command center with telemetry, strategy health, and portfolio signals.',
		'/data': 'Inspect datasets and data-quality health for supported markets.',
		'/all-trades': 'Full trade ledger across all statuses (open, closed, failed) with filtering and manual cleanup of phantom trades.',
		'/trading': 'Manage paper and live positions with manual controls, chart overlays, signals, and execution history.',
		'/risk': 'Monitor drawdown, kill-switch state, and live portfolio risk guardrails.',
		'/lab': 'Build, scan, and run the 24/7 autopilot lifecycle for strategy development.',
		'/hypotheses': 'Track market theses, linked strategies, source artifacts, and the missing data that blocks them.',
		'/agents': 'Review agent health, workloads, and orchestration status.',
		'/memory': 'Explore, curate, and audit cross-source AI memory across narrative, Chroma, and workspace logs.',
		'/tasks': 'Inspect task containers, ownership, status transitions, and execution audit trails.',
		'/approval': 'Review Brain proposals and approve, deny, or revise execution tasks.',
		'/diagnostics': 'Health checks, cost rollups, and resumable tasks for the Forven runtime.',
		'/integrations': 'Connect AI clients to Forven and manage external MCP tool servers for agents.',
		'/integrations/mcp': 'Connect AI clients to Forven and manage external MCP tool servers for agents.',
		'/settings': 'Configure execution, API keys, alerts, and platform preferences for Forven.',
	};

	function titleCase(value: string): string {
		return value
			.split(/[-_]/g)
			.filter(Boolean)
			.map((part) => part.charAt(0).toUpperCase() + part.slice(1))
			.join(' ');
	}

	function resolvePageTitle(pathname: string): string {
		if (TITLE_OVERRIDES[pathname]) return TITLE_OVERRIDES[pathname];
		const segments = pathname.split('/').filter(Boolean);
		if (!segments.length) return 'Dashboard';
		if (segments.some((segment) => segment.startsWith('['))) return 'Detail';
		const leaf = segments[segments.length - 1];
		if (/^\d+$/.test(leaf)) return `${titleCase(segments[segments.length - 2] ?? 'Detail')} Detail`;
		if (segments[0] === 'lab' && segments[1] === 'strategy') return 'Strategy Container';
		if (segments[0] === 'tasks' && segments.length >= 2) return 'Task Detail';
		if (segments[0] === 'integrations') return 'Integrations';
		return titleCase(leaf);
	}

	function resolvePageDescription(pathname: string): string {
		if (pathname.startsWith('/lab/strategy/')) return 'View a single strategy container dossier with lifecycle history and execution records.';
		if (pathname.startsWith('/tasks/') && pathname !== '/tasks/') return 'Inspect a single task container with audit trail, tool calls, and execution data.';
		if (pathname.startsWith('/integrations')) return 'Connect AI clients to Forven and manage external MCP tool servers for agents.';
		return DESCRIPTION_OVERRIDES[pathname] ?? 'Forven trading workspace.';
	}

	$: pageTitle = `${resolvePageTitle($page.url.pathname)} | Forven`;
	$: pageDescription = resolvePageDescription($page.url.pathname);
	$: unreadChatCountLabel = $chatUnreadCount > 9 ? '9+' : String($chatUnreadCount);
	// Publish the current route (+ inferred page kind) to the assistant on every
	// navigation. Pages enrich this with their entity/visible-data via setPageContext.
	$: setRoute($page.url.pathname);

	function shouldEnableLiveChannels(pathname: string): boolean {
		return !(pathname === '/settings' || pathname.startsWith('/settings/'));
	}

	function startPollers(): void {
		if (pollersActive) return;
		startHeartbeat();
		if (!processesBootstrapped) {
			bootstrapActiveProcesses({
				// Compatibility backend does not always expose `/jobs`; rely on
				// explicit trackProcess() calls instead of bootstrap recovery.
				includeJobs: false,
				includeScans: true,
				includeTournaments: false,
			});
			processesBootstrapped = true;
		}
		pollersActive = true;
	}

	function stopPollers(): void {
		if (!pollersActive) return;
		stopHeartbeat();
		pollersActive = false;
	}

	function startWsChannel(): void {
		if (wsChannelActive) return;
		connectForvenWs();
		wsChannelActive = true;
	}

	function stopWsChannel(): void {
		if (!wsChannelActive) return;
		disconnectForvenWs();
		wsChannelActive = false;
	}

	$: if (typeof window !== 'undefined' && connectionStatus === 'connected') {
		if (shouldEnableLiveChannels($page.url.pathname)) {
			startPollers();
		} else {
			stopPollers();
		}
	}

	$: if (typeof window !== 'undefined') {
		startWsChannel();
	}

	let healthRetryTimer: ReturnType<typeof setTimeout> | null = null;
	let healthRetryAttempts = 0;
	let healthFailureCount = 0;
	let lastHealthyAt = 0;

	async function attemptHealthCheck() {
		try {
			await checkHealth();
			backendConnected.set(true);
			connectionStatus = 'connected';
			healthRetryAttempts = 0;
			healthFailureCount = 0;
			lastHealthyAt = Date.now();
		} catch {
			const wsStillConnected = get(forvenWsConnected);
			healthFailureCount += 1;
			if (shouldMarkBackendDisconnected({
				wsStillConnected,
				consecutiveFailures: healthFailureCount,
				lastHealthyAt,
			})) {
				backendConnected.set(false);
				connectionStatus = 'disconnected';
			} else {
				connectionStatus = wsStillConnected ? 'connected' : 'checking';
			}
			// Retry with exponential backoff
			const delay = Math.min(1000 * Math.pow(2, healthRetryAttempts), 30000);
			healthRetryAttempts++;
			if (healthRetryTimer === null) {
				healthRetryTimer = setTimeout(() => {
					healthRetryTimer = null;
					attemptHealthCheck();
				}, delay);
			}
		}
	}

	function handleReconnect() {
		// On WS reconnect, re-check health and restart pollers
		connectionStatus = 'checking';
		attemptHealthCheck();
	}

	onMount(() => {
		startWsChannel();
		attemptHealthCheck();
		reloadWizardSettings().then(() => {
			if (wizardSettings?.setup_wizard_completed_at == null) {
				openWizard();
			}
		});
		if (typeof window !== 'undefined') {
			window.addEventListener('forven:reconnected', handleReconnect);
		}
	});

	onDestroy(() => {
		stopPollers();
		stopWsChannel();
		if (healthRetryTimer !== null) {
			clearTimeout(healthRetryTimer);
			healthRetryTimer = null;
		}
		if (typeof window !== 'undefined') {
			window.removeEventListener('forven:reconnected', handleReconnect);
		}
	});

</script>

<svelte:head>
	<title>{pageTitle}</title>
	<meta name="description" content={pageDescription} />
</svelte:head>

<div class="flex h-screen bg-black text-white font-mono overflow-hidden selection:bg-white selection:text-black">
	<Sidebar {connectionStatus} />

	<!-- Main Content -->
	<main class="flex-1 min-w-0 bg-black flex flex-col relative z-0">
		<RiskDisclaimerBanner />
		<UpdateBanner />
		<AgentProviderBanner />
		<LaunchBanner />
		<GlobalControlStrip />
		<div class="flex-1 min-h-0 overflow-y-auto overflow-x-hidden">
			<slot />
		</div>
	</main>
</div>

<PositionAlertWidget />

<Toast />

<!-- Floating Chat Button -->
<button
	on:click={toggleAssistant}
	class="fixed z-50 w-14 h-14 bg-cyan-600 hover:bg-cyan-500 rounded-full shadow-lg flex items-center justify-center transition-all duration-200 hover:scale-110 relative"
	style="position: fixed; right: 1.5rem; bottom: 1.5rem; left: auto; top: auto;"
	aria-label="Open assistant"
>
	{#if $assistantUI.open}
		<svg xmlns="http://www.w3.org/2000/svg" class="w-6 h-6" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
			<path stroke-linecap="round" stroke-linejoin="round" d="M6 18L18 6M6 6l12 12" />
		</svg>
	{:else}
		<svg xmlns="http://www.w3.org/2000/svg" class="w-6 h-6" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
			<path stroke-linecap="round" stroke-linejoin="round" d="M8 10h.01M12 10h.01M16 10h.01M9 16H5a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v8a2 2 0 01-2 2h-5l-5 5v-5z" />
		</svg>
	{/if}

	{#if !$assistantUI.open && $chatUnreadCount > 0}
		<span class="absolute -top-1 -right-1 h-5 w-5 rounded-full bg-rose-400 animate-ping opacity-75" aria-hidden="true"></span>
		<span
			class="absolute -top-1 -right-1 min-w-[1.25rem] h-5 px-1 rounded-full bg-rose-500 text-[10px] font-bold text-white flex items-center justify-center ring-2 ring-black"
			aria-label={`${$chatUnreadCount} unread chat replies`}
		>
			{unreadChatCountLabel}
		</span>
	{/if}
</button>

<AIChatPanel />

{#if wizardSettings && $wizardOpen}
	<SetupWizardModal settings={wizardSettings} />
{/if}

<!--
	SaveBar lives at the layout so the wizard (rendered above its own scrim at
	z-[100]) doesn't hide it. Wrapped in a z-[110] shim so the internal fixed
	bar beats the scrim. Invisible when dirtyFields is empty.
-->
<div class="relative z-[110]">
	<SettingsSaveBar currentValues={saveBarValues} />
</div>
