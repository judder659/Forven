<script lang="ts">
	import { onDestroy, onMount } from 'svelte';
	import { page } from '$app/stores';
	import { beforeNavigate, goto } from '$app/navigation';
	import {
		getForvenSchedulerJobs,
		getProviderHealth,
		updateForvenSchedulerJob,
		type ForvenSchedulerJob,
		type ProviderHealthResponse,
	} from '$lib/api';
	import {
		dismissAgentRun,
		forEachRun,
		getAgentActivity,
		getAgentFleet,
		getAgentYield,
		resumeAgentRun,
		setAgentEnabled,
		type ActivityRun,
		type AgentFleet,
		type AgentYield,
		type FleetAgent,
		type FleetWindow,
	} from '$lib/api/agentsHub';
	import { forvenWsConnected } from '$lib/stores/forvenWebSocket';
	import { addToast } from '$lib/stores/processTracker';
	import { createPoller, type Poller } from '$lib/utils/polling';
	import { createRealtimeRefresh, type RealtimeRefreshController } from '$lib/utils/realtime';
	import { buildAttention, type AttentionAction, type AttentionItem } from '$lib/utils/agentsHub/attention';
	import { agentJob } from '$lib/utils/agentsHub/agents';
	import { fmtCost, fmtRate, plural } from '$lib/utils/agentsHub/format';
	import { fleetHeadline } from '$lib/utils/agentsHub/headline';
	import { summarizeYield } from '$lib/utils/agentsHub/yield';
	import { ago } from '$lib/utils/forge/time';
	import DeskConfirmDialog, { type ConfirmSpec } from '$lib/components/trading/desk/DeskConfirmDialog.svelte';
	import ActivityFeed from '$lib/components/agents/ActivityFeed.svelte';
	import AgentDrawer from '$lib/components/agents/AgentDrawer.svelte';
	import AgentRoster from '$lib/components/agents/AgentRoster.svelte';
	import NeedsYouPanel from '$lib/components/agents/NeedsYouPanel.svelte';
	import RunsView from '$lib/components/agents/RunsView.svelte';
	import SchedulesView from '$lib/components/agents/SchedulesView.svelte';
	import YieldPanel from '$lib/components/agents/YieldPanel.svelte';
	import ProvidersTab from './components/tabs/ProvidersTab.svelte';
	import ModelsTab from './components/tabs/ModelsTab.svelte';
	import RoutingTab from './components/tabs/RoutingTab.svelte';
	import HealthTab from './components/tabs/HealthTab.svelte';
	import { agentsConfig } from './components/agentsConfigStore';

	// ---- Tabs (?tab=). Old ids keep working: roster → overview, tasks = Runs,
	// and each Setup section keeps its own id so deep links land on it.
	type Tab = 'overview' | 'tasks' | 'schedules' | 'providers' | 'models' | 'routing' | 'health';
	type SetupTab = 'providers' | 'models' | 'routing' | 'health';
	const SETUP: Array<{ id: SetupTab; label: string }> = [
		{ id: 'providers', label: 'Providers & keys' },
		{ id: 'models', label: 'Models' },
		{ id: 'routing', label: 'Routing & fallbacks' },
		{ id: 'health', label: 'Provider health' },
	];
	const SETUP_IDS = new Set<string>(SETUP.map((item) => item.id));
	const NAV: Array<{ id: 'overview' | 'tasks' | 'schedules' | 'setup'; label: string }> = [
		{ id: 'overview', label: 'Overview' },
		{ id: 'tasks', label: 'Runs' },
		{ id: 'schedules', label: 'Schedules' },
		{ id: 'setup', label: 'Setup' },
	];

	function normalizeTab(value: string | null): Tab {
		const tab = String(value ?? '').trim().toLowerCase();
		if (tab === 'tasks' || tab === 'runs') return 'tasks';
		if (tab === 'schedules') return 'schedules';
		if (SETUP_IDS.has(tab)) return tab as SetupTab;
		return 'overview';
	}

	$: activeTab = normalizeTab($page.url.searchParams.get('tab'));
	$: inSetup = SETUP_IDS.has(activeTab);
	let lastSetup: SetupTab = 'providers';
	$: if (inSetup) lastSetup = activeTab as SetupTab;

	// Setup's Routing and Models tabs unmount on switch, taking unsaved edits
	// with them; they report dirty state so switching away asks first.
	let routingDirty = false;
	let modelsDirty = false;
	$: setupDirty = (activeTab === 'routing' && routingDirty) || (activeTab === 'models' && modelsDirty);
	let leaveUrl: URL | null = null;
	let leaveConfirmed = false;

	function go(tab: string) {
		const target = tab === 'setup' ? lastSetup : tab;
		if (target === activeTab) return;
		if (setupDirty && !confirm('You have unsaved changes on this tab. Switch and discard them?')) return;
		routingDirty = false;
		modelsDirty = false;
		const url = new URL($page.url);
		for (const param of ['tab', 'status', 'agent', 'model', 'role']) url.searchParams.delete(param);
		if (target !== 'overview') url.searchParams.set('tab', target);
		void goto(`${url.pathname}${url.search}`, { keepFocus: true, noScroll: true });
	}

	beforeNavigate((navigation) => {
		if (leaveConfirmed) {
			leaveConfirmed = false;
			return;
		}
		if (!setupDirty) return;
		const to = navigation.to?.url ?? null;
		if (to && to.pathname === $page.url.pathname) return;
		navigation.cancel();
		leaveUrl = to;
	});

	function confirmLeave() {
		const url = leaveUrl;
		leaveUrl = null;
		if (!url) return;
		leaveConfirmed = true;
		void goto(url);
	}

	// ---- Window (24h / 7d), remembered per browser.
	const WINDOW_KEY = 'forven.agents.window';
	let windowKey: FleetWindow = '24h';
	function readWindow(): FleetWindow {
		try {
			return localStorage.getItem(WINDOW_KEY) === '7d' ? '7d' : '24h';
		} catch {
			return '24h';
		}
	}
	function setWindow(next: FleetWindow) {
		if (next === windowKey) return;
		windowKey = next;
		try {
			localStorage.setItem(WINDOW_KEY, next);
		} catch {
			// Private windows and blocked storage: the choice lasts this visit.
		}
		fleetLoading = true;
		yieldLoading = true;
		void loadFleet();
		void loadYield();
	}

	// ---- Data.
	let fleet: AgentFleet | null = null;
	let fleetLoading = true;
	let fleetError: string | null = null;
	let lastLoadedAt: string | null = null;
	let activity: ActivityRun[] = [];
	let activityLoading = true;
	let activityError: string | null = null;
	let yieldData: AgentYield | null = null;
	let yieldLoading = true;
	let yieldError: string | null = null;
	let providers: ProviderHealthResponse | null = null;
	let providersLoaded = false;
	let jobs: ForvenSchedulerJob[] = [];
	let jobsLoading = true;
	let jobsError: string | null = null;
	let now = Date.now();

	async function loadFleet() {
		const requested = windowKey;
		try {
			const next = await getAgentFleet(requested);
			if (requested !== windowKey) return;
			fleet = next;
			fleetError = null;
			lastLoadedAt = next.generated_at;
		} catch (error) {
			fleetError =
				error instanceof Error && /404|Not Found/i.test(error.message)
					? 'The fleet summary needs a newer backend. Restart the backend to load it.'
					: error instanceof Error
						? error.message
						: 'Could not load the agents.';
		} finally {
			fleetLoading = false;
		}
	}

	async function loadActivity() {
		try {
			activity = await getAgentActivity(40);
			activityError = null;
		} catch (error) {
			activityError = error instanceof Error ? error.message : 'Could not load recent runs.';
		} finally {
			activityLoading = false;
		}
	}

	async function loadYield() {
		const requested = windowKey;
		try {
			const next = await getAgentYield(requested === '7d' ? 7 : 1);
			if (requested !== windowKey) return;
			yieldData = next;
			yieldError = null;
		} catch (error) {
			yieldError = error instanceof Error ? error.message : 'Could not load what the agents produced.';
		} finally {
			yieldLoading = false;
		}
	}

	async function loadProviders() {
		try {
			providers = await getProviderHealth();
		} catch {
			// Provider health only adds items to "Needs you"; the page works without it.
		} finally {
			providersLoaded = true;
		}
	}

	async function loadJobs() {
		try {
			jobs = await getForvenSchedulerJobs();
			jobsError = null;
		} catch (error) {
			jobsError = error instanceof Error ? error.message : 'Could not load background jobs.';
		} finally {
			jobsLoading = false;
		}
	}

	async function refreshLive() {
		await Promise.all([loadFleet(), loadActivity()]);
	}

	async function refreshAll() {
		await Promise.all([loadFleet(), loadActivity(), loadYield(), loadProviders(), loadJobs()]);
	}

	let realtime: RealtimeRefreshController | null = null;
	let pollers: Poller[] = [];
	let clock: ReturnType<typeof setInterval> | null = null;
	let pollStart: ReturnType<typeof setTimeout> | null = null;

	onMount(() => {
		windowKey = readWindow();
		void refreshAll();
		void agentsConfig.load();
		realtime = createRealtimeRefresh(refreshLive, {
			fallbackMs: 30_000,
			wsDebounceMs: 1500,
			wsEvents: ['task_queued', 'task_status_changed', 'task_completed', 'task_failed', 'strategy_transition'],
		});
		realtime.start();
		pollers = [createPoller(loadProviders, 60_000), createPoller(loadJobs, 60_000), createPoller(loadYield, 300_000)];
		// start() fires at once and skips hidden tabs, so the first loads run above
		// and the pollers take over a minute later.
		pollStart = setTimeout(() => pollers.forEach((poller) => poller.start()), 60_000);
		clock = setInterval(() => (now = Date.now()), 5_000);
	});

	onDestroy(() => {
		realtime?.stop();
		if (pollStart) clearTimeout(pollStart);
		pollers.forEach((poller) => poller.stop());
		if (clock) clearInterval(clock);
	});

	// ---- Derived views.
	$: agents = fleet?.agents ?? [];
	$: names = Object.fromEntries(agents.map((agent) => [agent.id, agent.name]));
	$: attention = fleet ? buildAttention(fleet, providers, now) : [];
	$: yieldSummaries = yieldData ? summarizeYield(yieldData) : [];
	$: headline = fleet
		? fleetHeadline({
				fleet,
				yieldTop: yieldSummaries[0] ?? null,
				producerName: yieldSummaries[0] ? (names[yieldSummaries[0].agentId] ?? yieldSummaries[0].agentId) : null,
				attention: providersLoaded ? attention.length : null,
			})
		: [];
	$: judged = agents.reduce((sum, agent) => sum + agent.window.ok + agent.window.failed + agent.window.blocked, 0);
	$: workingNames = agents.filter((agent) => agent.state === 'running').map((agent) => agent.name);
	$: autonomy = fleet?.autonomy?.mode ?? null;

	// ---- Drawer (?agent= opens it, so a link can point at one agent).
	$: drawerId = activeTab === 'overview' ? ($page.url.searchParams.get('agent') ?? null) : null;
	$: drawerAgent = drawerId ? (agents.find((agent) => agent.id === drawerId) ?? null) : null;
	// The drawer lists every failure group for its agent, not the overview's top few.
	$: drawerAttention =
		drawerAgent && fleet
			? buildAttention(fleet, providers, now, Number.POSITIVE_INFINITY).filter((item) => item.agentId === drawerAgent?.id)
			: [];

	function openAgent(agentId: string) {
		const url = new URL($page.url);
		url.searchParams.set('agent', agentId);
		void goto(`${url.pathname}${url.search}`, { keepFocus: true, noScroll: true });
	}

	function closeAgent() {
		const url = new URL($page.url);
		url.searchParams.delete('agent');
		void goto(`${url.pathname}${url.search}`, { keepFocus: true, noScroll: true, replaceState: true });
	}

	// ---- Actions: every one asks first, says what happens, and reports every failure.
	let confirmSpec: ConfirmSpec | null = null;
	let confirmBusy = false;
	let attentionBusy: string | null = null;
	let toggleBusy: string | null = null;

	async function runConfirm() {
		if (!confirmSpec) return;
		confirmBusy = true;
		try {
			await confirmSpec.run();
			confirmSpec = null;
		} finally {
			confirmBusy = false;
		}
	}

	function reportBulk(verb: string, done: string, result: { ok: number; failed: Array<{ error: string }> }) {
		if (result.ok > 0) addToast(`${done} ${plural(result.ok, 'run')}.`, 'success');
		if (result.failed.length > 0) {
			addToast(`${plural(result.failed.length, 'run')} could not be ${verb}: ${result.failed[0].error}`, 'error', undefined, 9000);
		}
	}

	function onAttention(event: CustomEvent<{ item: AttentionItem; action: AttentionAction }>) {
		const { item, action } = event.detail;
		const ids = action.ids ?? [];
		const agentName = item.agentId ? (names[item.agentId] ?? item.agentId) : 'Several agents';
		if (action.kind === 'resume') {
			confirmSpec = {
				title: `Resume ${plural(ids.length, 'blocked run')}?`,
				warn: 'Each run continues from its saved checkpoint and uses model spend. Runs whose inputs are still blocked are refused and stay as they are.',
				rows: [['Agent', agentName], ['Runs', String(ids.length)], ['Stopped because', item.detail.slice(0, 80)]],
				cta: `Resume ${ids.length}`,
				run: async () => {
					attentionBusy = `${item.key}|resume`;
					try {
						reportBulk('resumed', 'Resumed', await forEachRun(ids, resumeAgentRun));
						await refreshLive();
					} finally {
						attentionBusy = null;
					}
				},
			};
		} else if (action.kind === 'dismiss') {
			confirmSpec = {
				title: `Dismiss ${plural(ids.length, 'run')}?`,
				warn: 'They leave Needs you and every attention count. The runs and their transcripts stay in the history.',
				rows: [['Agent', agentName], ['Runs', String(ids.length)], ['Reason', item.detail.slice(0, 80)]],
				cta: `Dismiss ${ids.length}`,
				run: async () => {
					attentionBusy = `${item.key}|dismiss`;
					try {
						reportBulk('dismissed', 'Dismissed', await forEachRun(ids, (id) => dismissAgentRun(id)));
						await refreshLive();
					} finally {
						attentionBusy = null;
					}
				},
			};
		} else if (action.kind === 'resume-agent' && action.agentId) {
			const agent = agents.find((candidate) => candidate.id === action.agentId);
			if (agent) askToggle(agent);
		}
	}

	function askToggle(agent: FleetAgent) {
		const pausing = agent.enabled;
		const job = agentJob(agent.id);
		confirmSpec = {
			title: pausing ? `Pause ${agent.name}?` : `Resume ${agent.name}?`,
			warn: pausing
				? `${job.pauseEffect} A run already in progress finishes.`
				: agent.pending > 0
					? `${plural(agent.pending, 'waiting run')} will start as capacity frees up.`
					: 'The agent takes new runs again.',
			rows: [
				['Working now', agent.running.length > 0 ? `“${agent.running[0].title}”` : 'nothing'],
				['Queued', String(agent.pending)],
			],
			cta: pausing ? 'Pause agent' : 'Resume agent',
			danger: pausing,
			run: async () => {
				toggleBusy = agent.id;
				try {
					await setAgentEnabled(agent.id, !pausing);
					addToast(pausing ? `${agent.name} paused.` : `${agent.name} resumed.`, 'success');
					await loadFleet();
				} catch (error) {
					addToast(error instanceof Error ? error.message : 'Could not change the agent.', 'error');
				} finally {
					toggleBusy = null;
				}
			},
		};
	}

	async function saveJob(jobId: string | number, scheduleType: string, scheduleExpr: string, enabled: boolean) {
		const result = await updateForvenSchedulerJob(jobId, scheduleType, scheduleExpr, enabled);
		if (!result?.ok) throw new Error(result?.error || 'The scheduler did not accept the change.');
		addToast('Schedule saved.', 'success');
		await Promise.all([loadJobs(), loadFleet()]);
	}

	let needsYouSection: HTMLElement;
	function showNeedsYou() {
		needsYouSection?.scrollIntoView({ behavior: 'smooth', block: 'center' });
	}
</script>

<svelte:head>
	<title>Agents | Forven</title>
	<meta name="description" content="What every agent is doing, how its runs go, what it produced, and what needs you." />
</svelte:head>

<div class="min-h-full bg-sc-bg">
	<header class="border-b border-sc-line bg-sc-panel">
		<div class="flex flex-wrap items-start justify-between gap-x-6 gap-y-3 px-5 pb-3 pt-4">
			<div class="min-w-0 max-w-[980px] flex-1">
				<div class="flex flex-wrap items-center gap-x-3 gap-y-1">
					<h1 class="text-[22px] font-semibold tracking-[-0.01em] text-sc-ink">Agents</h1>
					<span
						class="inline-flex items-center gap-1.5 rounded-full border border-sc-line2 px-2 py-0.5 text-[11px] text-sc-ink2"
						title={$forvenWsConnected ? 'Streaming task events; the page refreshes as runs start and finish.' : 'Live stream offline; the page polls instead.'}
					>
						<span class={`h-1.5 w-1.5 rounded-full ${$forvenWsConnected ? 'animate-pulse bg-[#3cc48f]' : 'bg-[#e7b24a]'}`} aria-hidden="true"></span>
						{$forvenWsConnected ? 'Live' : 'Polling'}{#if lastLoadedAt} · updated {ago(lastLoadedAt, now)}{/if}
					</span>
					{#if autonomy}
						<span class="inline-flex items-center rounded-full border border-sc-line2 px-2 py-0.5 text-[11px] text-sc-ink2" title="The autonomy mode in the top bar decides whether agents act on their own.">
							Autonomy: <span class="ml-1 text-sc-ink">{autonomy === 'semi_auto' ? 'Semi' : autonomy.charAt(0).toUpperCase() + autonomy.slice(1)}</span>
						</span>
					{/if}
				</div>
				<p class="m-0 mt-1.5 text-[13px] leading-relaxed text-sc-ink2" data-testid="agents-headline">
					{#if fleetError && !fleet}
						<span class="text-[#f2956f]">{fleetError}</span>
					{:else if !fleet}
						Reading the fleet…
					{:else}
						{#each headline as sentence, i (i)}
							{#if i === headline.length - 1 && providersLoaded && attention.length > 0}
								<button type="button" class="rounded bg-[#e7b24a]/10 px-1.5 py-px font-medium text-[#e7b24a] transition-colors hover:bg-[#e7b24a]/20" on:click={() => (activeTab === 'overview' ? showNeedsYou() : go('overview'))}>{sentence}</button>
							{:else}
								<span class={i === 0 ? 'text-sc-ink' : ''}>{sentence}</span>{' '}
							{/if}
						{/each}
					{/if}
				</p>
			</div>
			<div class="flex flex-wrap items-center gap-2">
				<div role="group" aria-label="Window" class="flex rounded-md border border-sc-line bg-sc-bg p-0.5">
					{#each ['24h', '7d'] as key (key)}
						<button
							type="button"
							aria-pressed={windowKey === key}
							class={`rounded px-2.5 py-0.5 text-[12px] transition-colors ${windowKey === key ? 'bg-sc-raise text-sc-ink' : 'text-sc-ink3 hover:text-sc-ink'}`}
							on:click={() => setWindow(key === '7d' ? '7d' : '24h')}
						>{key}</button>
					{/each}
				</div>
				<button type="button" class="rounded-md border border-sc-line2 px-3 py-1.5 text-[12px] text-sc-ink2 transition-colors hover:border-sc-ink hover:text-sc-ink" on:click={() => void refreshAll()}>Refresh</button>
				<a href="/settings" class="rounded-md border border-sc-line2 px-3 py-1.5 text-[12px] text-sc-ink2 transition-colors hover:border-sc-ink hover:text-sc-ink" title="App-wide settings: trading, notifications, data">App settings</a>
			</div>
		</div>
		<nav class="flex gap-1 px-5" aria-label="Agents sections">
			{#each NAV as item (item.id)}
				{@const current = item.id === 'setup' ? inSetup : activeTab === item.id}
				<button
					type="button"
					aria-current={current ? 'page' : undefined}
					class={`-mb-px border-b-2 px-3 py-2 text-[13px] transition-colors ${current ? 'border-sc-ink text-sc-ink' : 'border-transparent text-sc-ink3 hover:text-sc-ink'}`}
					on:click={() => go(item.id)}
				>
					{item.label}
					{#if item.id === 'tasks' && fleet && fleet.totals.blocked + fleet.totals.failed_open > 0}
						<span class="ml-1 rounded-full bg-[#e7b24a]/15 px-1.5 font-plex-mono text-[10.5px] text-[#e7b24a]">{(fleet.totals.blocked + fleet.totals.failed_open).toLocaleString('en-US')}</span>
					{/if}
					{#if item.id === 'schedules' && fleet && fleet.attention.scheduler.length > 0}
						<span class="ml-1 rounded-full bg-[#e5574f]/15 px-1.5 font-plex-mono text-[10.5px] text-[#f2956f]">{fleet.attention.scheduler.length}</span>
					{/if}
				</button>
			{/each}
		</nav>
	</header>

	<div class="px-5 pb-10 pt-4">
		{#if activeTab === 'overview'}
			<div class="grid gap-3">
				{#if fleetError && fleet}
					<p class="m-0 rounded-md border border-[#e7b24a]/40 bg-[#e7b24a]/10 px-3 py-2 text-[12px] text-[#e7b24a]" role="status">Could not refresh: {fleetError} Showing the last good read.</p>
				{/if}
				<div class="grid grid-cols-2 gap-px overflow-hidden rounded-md border border-sc-line bg-sc-line md:grid-cols-5" data-testid="agents-kpis">
					<div class="bg-sc-panel px-3.5 py-2.5">
						<p class="m-0 text-[11px] text-sc-ink3">Working now</p>
						<p class="m-0 mt-0.5 font-plex-mono text-[18px] text-sc-ink">{fleet ? fleet.totals.running : '—'}</p>
						<p class="m-0 truncate text-[11px] text-sc-ink3" title={workingNames.join(', ')}>{workingNames.length ? workingNames.join(', ') : 'nobody'}</p>
					</div>
					<div class="bg-sc-panel px-3.5 py-2.5">
						<p class="m-0 text-[11px] text-sc-ink3">Queued</p>
						<p class="m-0 mt-0.5 font-plex-mono text-[18px] text-sc-ink">{fleet ? fleet.totals.pending : '—'}</p>
						<p class="m-0 text-[11px] text-sc-ink3">{fleet && fleet.totals.paused > 0 ? `${plural(fleet.totals.paused, 'agent')} paused` : 'no agent paused'}</p>
					</div>
					<div class="bg-sc-panel px-3.5 py-2.5">
						<p class="m-0 text-[11px] text-sc-ink3">Runs · {windowKey}</p>
						<p class="m-0 mt-0.5 font-plex-mono text-[18px] text-sc-ink">{fleet ? fleet.totals.runs.toLocaleString('en-US') : '—'}</p>
						<p class="m-0 text-[11px] text-sc-ink3">{fleet && judged > 0 ? `${fmtRate(fleet.totals.ok / judged)} succeeded · ${fleet.totals.failed} failed` : 'none finished'}</p>
					</div>
					<div class="bg-sc-panel px-3.5 py-2.5">
						<p class="m-0 text-[11px] text-sc-ink3">Spend · {windowKey === '7d' ? '7 days' : 'today (UTC)'}</p>
						<p class="m-0 mt-0.5 font-plex-mono text-[18px] text-sc-ink">{fleet ? fmtCost(windowKey === '7d' ? fleet.totals.spend_d7 : fleet.totals.spend_today) : '—'}</p>
						<p class="m-0 text-[11px] text-sc-ink3">{fleet ? `${fmtCost(fleet.totals.spend_d30)} over 30 days` : ''}</p>
					</div>
					<button type="button" class="bg-sc-panel px-3.5 py-2.5 text-left transition-colors hover:bg-sc-hover" on:click={showNeedsYou}>
						<span class="block text-[11px] text-sc-ink3">Needs you</span>
						<span class={`mt-0.5 block font-plex-mono text-[18px] ${attention.length > 0 ? 'text-[#e7b24a]' : 'text-sc-ink'}`}>{fleet ? attention.length : '—'}</span>
						<span class="block text-[11px] text-sc-ink3">{fleet ? `${plural(fleet.totals.blocked, 'blocked run')} · ${fleet.totals.failed_open} failed` : ''}</span>
					</button>
				</div>

				<AgentRoster
					{agents}
					window={windowKey}
					{now}
					{autonomy}
					loading={fleetLoading}
					busyAgent={toggleBusy}
					on:open={(event) => openAgent(event.detail)}
					on:toggle={(event) => askToggle(event.detail)}
				/>

				<div class="grid gap-3 lg:h-[420px] lg:grid-cols-3" bind:this={needsYouSection}>
					<NeedsYouPanel items={attention} loading={fleetLoading} error={fleetError} busy={attentionBusy} on:action={onAttention} />
					<YieldPanel summaries={yieldSummaries} {names} loading={yieldLoading} error={yieldError} windowLabel={windowKey === '7d' ? '7 days' : '24 hours'} />
					<ActivityFeed runs={activity} {names} {now} loading={activityLoading} error={activityError} />
				</div>
			</div>
		{:else if activeTab === 'tasks'}
			<RunsView {fleet} {now} on:changed={() => void refreshLive()} />
		{:else if activeTab === 'schedules'}
			<SchedulesView {jobs} {now} loading={jobsLoading} error={jobsError} onSave={saveJob} />
		{:else}
			<div class="grid gap-4 lg:grid-cols-[200px_minmax(0,1fr)]">
				<nav class="flex gap-1 overflow-x-auto lg:flex-col" aria-label="Setup sections">
					{#each SETUP as item (item.id)}
						<button
							type="button"
							aria-current={activeTab === item.id ? 'page' : undefined}
							class={`whitespace-nowrap rounded-md px-3 py-1.5 text-left text-[12.5px] transition-colors ${activeTab === item.id ? 'bg-sc-raise text-sc-ink' : 'text-sc-ink3 hover:bg-sc-hover hover:text-sc-ink'}`}
							on:click={() => go(item.id)}
						>{item.label}</button>
					{/each}
					<a href="/agents/toolsets" class="whitespace-nowrap rounded-md px-3 py-1.5 text-[12.5px] text-sc-ink3 transition-colors hover:bg-sc-hover hover:text-sc-ink">Tool permissions ↗</a>
					<a href="/integrations/mcp" class="whitespace-nowrap rounded-md px-3 py-1.5 text-[12.5px] text-sc-ink3 transition-colors hover:bg-sc-hover hover:text-sc-ink">MCP servers ↗</a>
				</nav>
				<div class="min-w-0">
					{#if activeTab === 'providers'}
						<ProvidersTab />
					{:else if activeTab === 'models'}
						<ModelsTab onDirtyChange={(dirty) => (modelsDirty = dirty)} />
					{:else if activeTab === 'routing'}
						<RoutingTab onDirtyChange={(dirty) => (routingDirty = dirty)} />
					{:else if activeTab === 'health'}
						<HealthTab />
					{/if}
				</div>
			</div>
		{/if}
	</div>
</div>

{#if drawerAgent}
	<AgentDrawer
		agent={drawerAgent}
		attention={drawerAttention}
		{attentionBusy}
		toggleBusy={toggleBusy === drawerAgent.id}
		window={windowKey}
		{now}
		{autonomy}
		on:close={closeAgent}
		on:toggle={(event) => askToggle(event.detail)}
		on:action={onAttention}
		on:changed={() => void refreshLive()}
	/>
{/if}

{#if confirmSpec}
	<DeskConfirmDialog spec={confirmSpec} busy={confirmBusy} on:cancel={() => (confirmSpec = null)} on:confirm={runConfirm} />
{/if}

{#if leaveUrl}
	<DeskConfirmDialog
		spec={{ title: 'Discard unsaved changes?', warn: 'You have unsaved changes on this tab. Leaving the page discards them.', rows: [], cta: 'Discard and leave', danger: true, run: async () => confirmLeave() }}
		on:cancel={() => (leaveUrl = null)}
		on:confirm={confirmLeave}
	/>
{/if}
