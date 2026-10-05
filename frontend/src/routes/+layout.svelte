<script lang="ts">
	import '../app.css';
	// The app's type: IBM Plex, bundled so every page renders the same offline.
	import '@fontsource/ibm-plex-sans/latin-400.css';
	import '@fontsource/ibm-plex-sans/latin-500.css';
	import '@fontsource/ibm-plex-sans/latin-600.css';
	import '@fontsource/ibm-plex-sans-condensed/latin-500.css';
	import '@fontsource/ibm-plex-sans-condensed/latin-600.css';
	import '@fontsource/ibm-plex-mono/latin-400.css';
	import '@fontsource/ibm-plex-mono/latin-500.css';
	import { page } from '$app/stores';
	import { onMount, onDestroy } from 'svelte';
	import { get } from 'svelte/store';
	import { checkHealth, getSettings } from '$lib/api';
	import { backendConnected } from '$lib/stores';
	import { bootstrapActiveProcesses } from '$lib/stores/processTracker';
	import { startHeartbeat, stopHeartbeat } from '$lib/stores/heartbeat';
	import { connectForvenWs, disconnectForvenWs, forvenWsConnected } from '$lib/stores/forvenWebSocket';
	import { startNotificationRouter, stopNotificationRouter } from '$lib/stores/notificationRouter';
	import { loadNotificationPrefs } from '$lib/stores/notificationPrefs';
	import { shouldMarkBackendDisconnected } from '$lib/utils/connectionHealth';
	import Sidebar from '$lib/components/Sidebar.svelte';
	import Toast from '$lib/components/Toast.svelte';
	import GlobalControlStrip from '$lib/components/forven/GlobalControlStrip.svelte';
	import LaunchBanner from '$lib/components/LaunchBanner.svelte';
	import RiskDisclaimerBanner from '$lib/components/RiskDisclaimerBanner.svelte';
	import AgentProviderBanner from '$lib/components/AgentProviderBanner.svelte';
	import ThroughputSuggestionBanner from '$lib/components/ThroughputSuggestionBanner.svelte';
	import ConnectionHealthBanner from '$lib/components/ConnectionHealthBanner.svelte';
	import UpdateBanner from '$lib/components/UpdateBanner.svelte';
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
		'/paper-trades': 'Paper Trades',
		'/live-trades': 'Live Trades',
		'/risk': 'Risk',
		'/lab': 'The Forge',
		'/agents': 'Agents',
		'/approval': 'Approvals',
		'/diagnostics': 'Diagnostics',
		'/integrations': 'Integrations',
		'/integrations/mcp': 'Integrations',
		'/settings': 'Settings',
	};

	const DESCRIPTION_OVERRIDES: Record<string, string> = {
		'/': 'Live trading command center with telemetry, strategy health, and portfolio signals.',
		'/data': 'Market data health, every stored series, downloads, jobs and storage.',
		'/all-trades': 'Full trade ledger across all statuses (open, closed, failed) with filtering and manual cleanup of phantom trades.',
		'/paper-trades': 'Monitor paper trading sessions with manual controls, chart overlays, signals, and execution history — simulated fills only.',
		'/live-trades': 'Manage live positions of deployed strategies with manual controls — actions here drive REAL exchange orders.',
		'/risk': 'Monitor drawdown, kill-switch state, and live portfolio risk guardrails.',
		'/lab': 'Build, scan, and run the 24/7 autopilot lifecycle for strategy development.',
		'/agents': 'Review agent health, workloads, and orchestration status.',
		'/approval': 'Review Brain proposals and approve, deny, or revise agent runs.',
		'/diagnostics': 'Health checks, cost rollups, and resumable runs for the Forven runtime.',
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
		if (segments[0] === 'tasks' && segments.length >= 2) return 'Run Detail';
		if (segments[0] === 'integrations') return 'Integrations';
		return titleCase(leaf);
	}

	function resolvePageDescription(pathname: string): string {
		if (pathname.startsWith('/lab/strategy/')) return 'View a single strategy container dossier with lifecycle history and execution records.';
		if (pathname.startsWith('/tasks/') && pathname !== '/tasks/') return 'Inspect a single agent run with its audit trail, tool calls, and execution data.';
		if (pathname.startsWith('/integrations')) return 'Connect AI clients to Forven and manage external MCP tool servers for agents.';
		return DESCRIPTION_OVERRIDES[pathname] ?? 'Forven trading workspace.';
	}

	$: pageTitle = `${resolvePageTitle($page.url.pathname)} | Forven`;
	$: pageDescription = resolvePageDescription($page.url.pathname);
	$: unreadChatCountLabel = $chatUnreadCount > 9 ? '9+' : String($chatUnreadCount);
	// Publish the current route (+ inferred page kind) to the assistant on every
	// navigation. Pages enrich this with their entity/visible-data via setPageContext.
	$: setRoute($page.url.pathname);

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

	// The heartbeat runs on every page, Settings included: it is the only source
	// of the sidebar badge counts, which otherwise froze (or, on a fresh load,
	// vanished) while Settings was open.
	$: if (typeof window !== 'undefined' && connectionStatus === 'connected') {
		startPollers();
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
		// The backend may have restarted with switches changed elsewhere.
		void loadNotificationPrefs();
	}

	onMount(() => {
		startWsChannel();
		void loadNotificationPrefs();
		startNotificationRouter();
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
		stopNotificationRouter();
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

<!-- When the assistant is open, the app shell pads right by the panel width so
     the page pushes over and stays usable next to the chat (no dimming overlay).
     min(440px, 92vw) mirrors the panel's w-[440px] max-w-[92vw]. -->
<div
	class="flex h-screen bg-sc-bg text-sc-ink font-sans overflow-hidden selection:bg-sc-ink selection:text-black"
	style="transition: padding-right 250ms ease;"
	style:padding-right={$assistantUI.open ? 'min(440px, 92vw)' : '0px'}
>
	<Sidebar {connectionStatus} />

	<!-- Main Content: z-0 keeps every page z-index, modals included, below the
	     fixed chrome after it (chat button, toasts, assistant, wizard, save bar).
	     The sidebar has no z-index and comes first, so page overlays cover it. -->
	<main class="flex-1 min-w-0 bg-sc-bg flex flex-col relative z-0">
		<RiskDisclaimerBanner />
		<UpdateBanner />
		<AgentProviderBanner />
		<ThroughputSuggestionBanner />
		<ConnectionHealthBanner />
		<LaunchBanner />
		<GlobalControlStrip />
		<div class="flex-1 min-h-0 overflow-y-auto overflow-x-hidden">
			<slot />
		</div>
	</main>
</div>

<!-- Bottom-right pop-up stack (which events pop up: Settings → Notifications).
     Sits above the floating chat button and shifts left of the assistant panel
     when it's open. -->
<div
	class="fixed bottom-24 z-[9999] flex flex-col items-end gap-2 pointer-events-none"
	style="transition: right 250ms ease;"
	style:right={$assistantUI.open ? 'calc(1rem + min(440px, 92vw))' : '1rem'}
>
	<Toast />
</div>

<!-- Floating Chat Button: slides left of the panel when it's open so it stays a toggle. -->
<button
	on:click={toggleAssistant}
	class="rounded-md fixed z-50 w-14 h-14 border border-sc-line2 bg-sc-bg text-sc-ink hover:bg-sc-ink hover:text-black flex items-center justify-center relative"
	style="position: fixed; bottom: 1.5rem; left: auto; top: auto; transition: right 250ms ease, background-color 150ms ease, color 150ms ease;"
	style:right={$assistantUI.open ? 'calc(1.5rem + min(440px, 92vw))' : '1.5rem'}
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
		<span
			class="absolute -top-1 -right-1 min-w-[1.25rem] h-5 px-1 bg-red-500 text-[10px] font-bold text-sc-ink flex items-center justify-center"
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
