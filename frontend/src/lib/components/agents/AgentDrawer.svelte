<script lang="ts">
	import { createEventDispatcher, onMount, tick } from 'svelte';
	import {
		getAgentRow,
		getAgentWorkspace,
		renameAgent,
		type AgentWorkspace,
		type FleetAgent,
		type FleetWindow,
	} from '$lib/api/agentsHub';
	import { deleteForvenAgent, testForvenAgentDiscord, updateForvenAgent, updateForvenAgentDocument } from '$lib/api';
	import { grantMCPServer, listMCPGrants, listMCPServers, revokeMCPServer, type MCPGrant, type MCPServer } from '$lib/api/mcp';
	import { addToast } from '$lib/stores/processTracker';
	import type { AttentionAction, AttentionItem } from '$lib/utils/agentsHub/attention';
	import { STATE_META, agentJob, canPause, fallbackNote, modelParts, stateLine, typeLabel } from '$lib/utils/agentsHub/agents';
	import type { FailoverContext } from '$lib/utils/agentsHub/failover';
	import { fmtCost, fmtRate, fmtSeconds, fmtTokens, plural } from '$lib/utils/agentsHub/format';
	import { TONE_PILL, TONE_TEXT } from '$lib/utils/forge/status';
	import { ago, parseUtc, shortDateTime } from '$lib/utils/forge/time';
	import NeedsYouPanel from './NeedsYouPanel.svelte';
	import Sparkbars from './Sparkbars.svelte';

	export let agent: FleetAgent;
	export let attention: AttentionItem[] = [];
	export let attentionBusy: string | null = null;
	export let toggleBusy = false;
	export let window: FleetWindow = '24h';
	export let now = Date.now();
	export let autonomy: string | null = null;
	/** Fallback chains, backup and connected providers; null until loaded. */
	export let failover: FailoverContext | null = null;

	const dispatch = createEventDispatcher<{
		close: void;
		toggle: FleetAgent;
		action: { item: AttentionItem; action: AttentionAction };
		changed: void;
	}>();

	type Tab = 'overview' | 'runs' | 'memory' | 'instructions' | 'tools' | 'settings';
	type DocKey = 'role' | 'soul' | 'agents';
	const TABS: Array<{ id: Tab; label: string }> = [
		{ id: 'overview', label: 'Overview' },
		{ id: 'runs', label: 'Runs' },
		{ id: 'memory', label: 'Memory' },
		{ id: 'instructions', label: 'Instructions' },
		{ id: 'tools', label: 'Tools' },
		{ id: 'settings', label: 'Settings' },
	];
	const DOCS: Array<{ key: DocKey; file: string; label: string; help: string }> = [
		{ key: 'role', file: 'ROLE.md', label: 'Mandate', help: 'What this agent is for and how it works. Read first, at the start of every run.' },
		{ key: 'soul', file: 'SOUL.md', label: 'Identity', help: 'Who the agent is and the principles it holds to.' },
		{ key: 'agents', file: 'AGENTS.md', label: 'Operating guide', help: 'How to use its workspace, tools and teammates.' },
	];

	let tab: Tab = 'overview';
	let workspace: AgentWorkspace | null = null;
	let workspaceError: string | null = null;
	let workspaceLoading = false;

	let docKey: DocKey = 'role';
	let drafts: Record<DocKey, string> = { role: '', soul: '', agents: '' };
	let saved: Record<DocKey, string> = { role: '', soul: '', agents: '' };
	let docSaving = false;

	let servers: MCPServer[] = [];
	let grants: MCPGrant[] = [];
	let mcpLoaded = false;
	let mcpLoading = false;
	let mcpError: string | null = null;
	let mcpBusy: string | null = null;

	let nameDraft = agent.name;
	let nameSaving = false;
	let hasDiscordToken = false;
	let discordToken = '';
	let discordSaving = false;
	let discordTesting = false;
	let deleting = false;
	let closeButton: HTMLButtonElement;

	$: meta = STATE_META[agent.state];
	$: job = agentJob(agent.id);
	$: model = modelParts(agent);
	$: fallback = fallbackNote(agent, failover);
	$: stats = agent.window;
	$: windowWords = window === '7d' ? 'the last 7 days' : 'the last 24 hours';
	$: types = Object.entries(stats.types);
	$: dirtyDocs = (Object.keys(drafts) as DocKey[]).filter((key) => drafts[key] !== saved[key]);
	$: activeDoc = DOCS.find((doc) => doc.key === docKey) ?? DOCS[0];
	// Once per visit to the tab: a failed load shows its error and waits for Reload.
	$: if (tab === 'tools' && !mcpLoaded && !mcpLoading && !mcpError) void loadMcp();
	$: grantedNames = new Set(grants.map((grant) => grant.server_name));

	async function loadWorkspace() {
		mcpError = null;
		mcpLoaded = false;
		workspaceLoading = true;
		workspaceError = null;
		try {
			const next = await getAgentWorkspace(agent.id);
			workspace = next;
			const docs = { role: next.documents?.role ?? '', soul: next.documents?.soul ?? '', agents: next.documents?.agents ?? '' };
			// Keep an edit in progress; refresh only the documents left untouched.
			for (const key of Object.keys(docs) as DocKey[]) {
				if (drafts[key] === saved[key]) drafts[key] = docs[key];
			}
			drafts = { ...drafts };
			saved = docs;
		} catch (error) {
			workspaceError = error instanceof Error ? error.message : 'Could not load this agent’s workspace.';
		} finally {
			workspaceLoading = false;
		}
	}

	async function loadRow() {
		try {
			const row = await getAgentRow(agent.id);
			hasDiscordToken = Boolean(row.has_discord_token);
		} catch {
			// The settings tab still works without the token flag.
		}
	}

	async function loadMcp() {
		mcpLoading = true;
		mcpError = null;
		try {
			const [serverList, grantList] = await Promise.all([listMCPServers(), listMCPGrants(agent.id)]);
			servers = serverList.servers ?? [];
			grants = grantList.grants ?? [];
			mcpLoaded = true;
		} catch (error) {
			mcpError = error instanceof Error ? error.message : 'Could not load MCP servers.';
		} finally {
			mcpLoading = false;
		}
	}

	async function toggleGrant(server: string, granted: boolean) {
		mcpBusy = server;
		try {
			if (granted) await revokeMCPServer(agent.id, server);
			else await grantMCPServer(agent.id, server);
			await loadMcp();
		} catch (error) {
			addToast(error instanceof Error ? error.message : 'Could not change the grant.', 'error');
		} finally {
			mcpBusy = null;
		}
	}

	async function saveDoc() {
		const key = docKey;
		docSaving = true;
		try {
			await updateForvenAgentDocument(agent.id, key, drafts[key]);
			saved = { ...saved, [key]: drafts[key] };
			addToast(`${activeDoc.file} saved. The next run reads it.`, 'success');
		} catch (error) {
			addToast(error instanceof Error ? error.message : `Could not save ${activeDoc.file}.`, 'error');
		} finally {
			docSaving = false;
		}
	}

	async function saveName() {
		const name = nameDraft.trim();
		if (!name || name === agent.name) return;
		nameSaving = true;
		try {
			await renameAgent(agent.id, name);
			addToast(`Renamed to “${name}”.`, 'success');
			dispatch('changed');
		} catch (error) {
			addToast(error instanceof Error ? error.message : 'Could not rename the agent.', 'error');
		} finally {
			nameSaving = false;
		}
	}

	async function saveDiscordToken() {
		const token = discordToken.trim();
		if (!token) return;
		discordSaving = true;
		try {
			const updated = await updateForvenAgent(agent.id, { discord_token: token });
			hasDiscordToken = Boolean(updated.has_discord_token ?? true);
			discordToken = '';
			addToast('Discord bot token saved.', 'success');
		} catch (error) {
			addToast(error instanceof Error ? error.message : 'Could not save the token.', 'error');
		} finally {
			discordSaving = false;
		}
	}

	async function testDiscord() {
		discordTesting = true;
		try {
			const result = await testForvenAgentDiscord(agent.id, discordToken.trim() || undefined);
			addToast(`Test message sent to #${result.channel}.`, 'success');
		} catch (error) {
			addToast(error instanceof Error ? error.message : 'The test message failed.', 'error');
		} finally {
			discordTesting = false;
		}
	}

	async function deleteAgent() {
		// forven.agents.manager.delete_agent removes the agent's runs with it.
		if (!confirm(`Delete ${agent.name}? Its runs and their history are deleted with it. Its memory and documents stay on disk.`)) return;
		deleting = true;
		try {
			await deleteForvenAgent(agent.id);
			addToast(`${agent.name} deleted.`, 'success');
			dispatch('changed');
			dispatch('close');
		} catch (error) {
			addToast(error instanceof Error ? error.message : 'Could not delete the agent.', 'error');
		} finally {
			deleting = false;
		}
	}

	function requestClose() {
		if (dirtyDocs.length > 0 && !confirm('You have unsaved instruction changes. Close and discard them?')) return;
		dispatch('close');
	}

	function onKey(event: KeyboardEvent) {
		if (event.key === 'Escape') requestClose();
	}

	function runStatusTone(status: string): keyof typeof TONE_TEXT {
		const value = status.toLowerCase();
		if (value === 'done' || value === 'completed' || value === 'reviewed') return 'ok';
		if (value === 'failed' || value === 'error') return 'fail';
		if (value === 'blocked') return 'caution';
		if (value === 'running') return 'info';
		if (value === 'pending' || value === 'paused_manual') return 'wait';
		return 'idle';
	}

	function runSeconds(start: string | null, end: string | null): number | null {
		const a = parseUtc(start);
		const b = parseUtc(end);
		return a !== null && b !== null && b >= a ? (b - a) / 1000 : null;
	}

	function logTone(level: string): string {
		const value = level.toLowerCase();
		if (value === 'error' || value === 'critical') return 'text-[#f2956f]';
		if (value === 'warning' || value === 'warn') return 'text-[#e7b24a]';
		return 'text-sc-ink2';
	}

	onMount(async () => {
		void loadWorkspace();
		void loadRow();
		await tick();
		closeButton?.focus();
	});
</script>

<svelte:window on:keydown={onKey} />

<div class="fixed inset-0 z-[60] flex justify-end bg-[#040507]/60" role="presentation" on:click={(event) => { if (event.target === event.currentTarget) requestClose(); }}>
	<div
		class="flex h-full w-full max-w-[780px] flex-col border-l border-sc-line bg-sc-bg shadow-2xl"
		role="dialog"
		aria-modal="true"
		aria-label={`${agent.name} details`}
		data-testid="agent-drawer"
	>
		<header class="border-b border-sc-line bg-sc-panel px-5 pb-0 pt-4">
			<div class="flex items-start justify-between gap-4">
				<div class="min-w-0">
					<div class="flex flex-wrap items-center gap-x-2.5 gap-y-1">
						<h2 class="text-[18px] font-semibold text-sc-ink">{agent.name}</h2>
						<span class={`inline-flex items-center rounded border px-1.5 py-px text-[11px] ${TONE_PILL[meta.tone]}`}>{meta.label}</span>
						<span class="text-[12px] text-sc-ink3">{job.job}</span>
					</div>
					<p class="m-0 mt-1 text-[12.5px] leading-relaxed text-sc-ink2">{agent.role}</p>
					<p class="m-0 mt-1 text-[11.5px] text-sc-ink3">
						<span class="font-plex-mono">{agent.id}</span> · <span class="font-plex-mono text-sc-ink2">{model.model}</span> on {model.provider}
					</p>
				</div>
				<div class="flex shrink-0 items-center gap-1.5">
					<button type="button" class="rounded-md border border-sc-line2 px-2.5 py-1 text-[12px] text-sc-ink2 hover:border-sc-ink hover:text-sc-ink disabled:opacity-40" disabled={workspaceLoading} title="Reload memory, documents and runs" on:click={() => void loadWorkspace()}>{workspaceLoading ? 'Loading…' : 'Reload'}</button>
					{#if canPause(agent)}
						<button
							type="button"
							class={`rounded-md border px-2.5 py-1 text-[12px] transition-colors disabled:opacity-50 ${agent.enabled ? 'border-sc-line2 text-sc-ink2 hover:border-sc-ink3 hover:text-sc-ink' : 'border-[#7fb2ff]/50 text-[#a9cbff] hover:border-[#7fb2ff]'}`}
							disabled={toggleBusy}
							title={agent.enabled ? job.pauseEffect : 'Let this agent take runs again'}
							on:click={() => dispatch('toggle', agent)}
						>{toggleBusy ? 'Working…' : agent.enabled ? 'Pause' : 'Resume'}</button>
					{/if}
					<button bind:this={closeButton} type="button" class="rounded-md border border-sc-line2 px-2 py-1 text-[12px] text-sc-ink2 hover:border-sc-ink hover:text-sc-ink" aria-label="Close" on:click={requestClose}>✕</button>
				</div>
			</div>
			<nav class="-mb-px mt-3 flex gap-1 overflow-x-auto" aria-label="Agent sections">
				{#each TABS as item (item.id)}
					<button
						type="button"
						aria-current={tab === item.id ? 'page' : undefined}
						class={`whitespace-nowrap border-b-2 px-3 py-2 text-[12.5px] transition-colors ${tab === item.id ? 'border-sc-ink text-sc-ink' : 'border-transparent text-sc-ink3 hover:text-sc-ink'}`}
						on:click={() => (tab = item.id)}
					>
						{item.label}
						{#if item.id === 'instructions' && dirtyDocs.length > 0}<span class="ml-1 text-[#e7b24a]">●</span>{/if}
					</button>
				{/each}
			</nav>
		</header>

		<div class="min-h-0 flex-1 overflow-y-auto px-5 py-4">
			{#if tab === 'overview'}
				<div class="grid gap-4">
					<p class="m-0 text-[12.5px] text-sc-ink2">{stateLine(agent, now)}</p>
					{#if agent.id === 'brain'}
						<p class="m-0 rounded-md border border-sc-line bg-sc-panel px-3 py-2 text-[12px] text-sc-ink2">
							Brain cycles follow the autonomy mode{#if autonomy}{' '}(now <span class="text-sc-ink">{autonomy === 'semi_auto' ? 'Semi' : autonomy.charAt(0).toUpperCase() + autonomy.slice(1)}</span>){/if}. Switch Manual · Semi · Auto in the top bar to stop or start them.
						</p>
					{/if}
					<dl class="m-0 grid grid-cols-2 gap-px overflow-hidden rounded-md border border-sc-line bg-sc-line sm:grid-cols-4">
						{#each [
							[`Runs · ${window}`, stats.runs.toLocaleString('en-US'), `${stats.ok} done · ${stats.failed} failed · ${stats.blocked} blocked`],
							['Succeeded', fmtRate(stats.success_rate), 'cancelled runs not counted'],
							['Median run', fmtSeconds(stats.median_seconds), 'successful runs'],
							[window === '7d' ? 'Spend · 7d' : 'Spend · today', fmtCost(window === '7d' ? agent.spend.d7 : agent.spend.today), `${fmtCost(agent.spend.d30)} over 30 days`],
						] as [label, value, sub] (label)}
							<div class="bg-sc-panel px-3 py-2.5">
								<dt class="text-[11px] text-sc-ink3">{label}</dt>
								<dd class="m-0 mt-0.5 font-plex-mono text-[16px] text-sc-ink">{value}</dd>
								<dd class="m-0 text-[11px] text-sc-ink3">{sub}</dd>
							</div>
						{/each}
					</dl>
					<section class="rounded-md border border-sc-line bg-sc-panel px-3.5 py-3">
						<div class="mb-2 flex items-baseline justify-between gap-2">
							<h3 class="m-0 text-[12.5px] font-medium text-sc-ink">Runs over {windowWords}</h3>
							<span class="text-[11px] text-sc-ink3">{fmtTokens(stats.tokens)} tokens</span>
						</div>
						<Sparkbars buckets={stats.buckets} height={48} barWidth={Math.max(6, Math.floor(680 / Math.max(1, stats.buckets.length)) - 3)} gap={3} label={`${plural(stats.runs, 'run')} over ${windowWords}`} />
						<div class="mt-1 flex justify-between text-[10.5px] text-sc-ink4"><span>{window === '7d' ? '7 days ago' : '24 hours ago'}</span><span>now</span></div>
						{#if types.length > 0}
							<div class="mt-3 flex flex-wrap gap-1.5">
								{#each types as [type, count] (type)}
									<span class="rounded border border-sc-line2 bg-sc-panel2 px-1.5 py-0.5 text-[11px] text-sc-ink2">{count.toLocaleString('en-US')} {typeLabel(type, count)}</span>
								{/each}
							</div>
						{/if}
					</section>
					{#if attention.length > 0}
						<div class="max-h-[340px]">
							<NeedsYouPanel items={attention} busy={attentionBusy} on:action={(event) => dispatch('action', event.detail)} />
						</div>
					{/if}
					{#if agent.last}
						<section class="rounded-md border border-sc-line bg-sc-panel px-3.5 py-3 text-[12px]">
							<h3 class="m-0 mb-1 text-[12.5px] font-medium text-sc-ink">Last finished {agent.id === 'brain' ? 'cycle' : 'run'}</h3>
							<p class="m-0 text-sc-ink2">
								<span class={TONE_TEXT[runStatusTone(agent.last.status)]}>{agent.last.status}</span> · {agent.last.title} · {ago(agent.last.completed_at, now)}
							</p>
							{#if agent.last.error}<p class="m-0 mt-1 font-plex-mono text-[11px] text-[#f2956f]">{agent.last.error}</p>{/if}
						</section>
					{/if}
				</div>
			{:else if tab === 'runs'}
				{#if workspaceError}
					<p class="text-[12px] text-[#f2956f]">{workspaceError}</p>
				{:else if !workspace}
					<p class="text-[12px] text-sc-ink3">Loading runs…</p>
				{:else if workspace.runs.length === 0}
					<p class="text-[12px] text-sc-ink3">{agent.id === 'brain' ? 'Brain cycles are listed on the Brain page; this agent has no other runs yet.' : 'No runs yet.'}</p>
				{:else}
					<div class="mb-2 flex items-center justify-between text-[11px] text-sc-ink3">
						<span>The latest {workspace.runs.length} runs. Open one for its full transcript.</span>
						<a class="hover:text-sc-ink" href={`/agents?tab=tasks&agent=${encodeURIComponent(agent.id)}`}>All runs →</a>
					</div>
					<ol class="m-0 list-none divide-y divide-sc-line overflow-hidden rounded-md border border-sc-line bg-sc-panel p-0">
						{#each workspace.runs as run (run.id)}
							{@const seconds = runSeconds(run.started_at, run.completed_at)}
							<li class="grid grid-cols-[minmax(0,1fr)_auto] gap-3 px-3 py-2 hover:bg-sc-hover">
								<div class="min-w-0">
									<a class="block truncate text-[12px] text-sc-ink hover:underline" href={`/tasks/${encodeURIComponent(run.display_id ?? `T${run.id}`)}?returnTo=${encodeURIComponent('/agents')}`}>{run.title || typeLabel(run.type)}</a>
									<span class="block truncate text-[11px] text-sc-ink3">
										<span class={TONE_TEXT[runStatusTone(run.status)]}>{run.status}</span> · {typeLabel(run.type)}{seconds !== null ? ` · ${fmtSeconds(seconds)}` : ''}{run.total_tokens ? ` · ${fmtTokens(run.total_tokens)} tok` : ''}{run.cost_usd ? ` · ${fmtCost(run.cost_usd)}` : ''}{run.model_id ? ` · ${run.model_id}` : ''}
									</span>
									{#if run.error}<span class="block truncate font-plex-mono text-[11px] text-[#f2956f]" title={run.error}>{run.error}</span>{/if}
								</div>
								<span class="whitespace-nowrap text-[11px] text-sc-ink3" title={shortDateTime(run.completed_at ?? run.created_at)}>{ago(run.completed_at ?? run.started_at ?? run.created_at, now)}</span>
							</li>
						{/each}
					</ol>
				{/if}
			{:else if tab === 'memory'}
				<div class="grid gap-3">
					<p class="m-0 text-[12px] text-sc-ink2">
						<span class="font-plex-mono text-sc-ink">agents/{agent.id}/memory/MEMORY.md</span> is added to every run’s context. The agent writes it as it learns; a wrong lesson here steers every later run.
					</p>
					{#if workspaceError}
						<p class="text-[12px] text-[#f2956f]">{workspaceError}</p>
					{:else if !workspace}
						<p class="text-[12px] text-sc-ink3">Loading memory…</p>
					{:else}
						{#if workspace.memory.trim()}
							<pre class="m-0 max-h-[52vh] overflow-auto whitespace-pre-wrap break-words rounded-md border border-sc-line bg-sc-panel p-3 font-plex-mono text-[11.5px] leading-[1.55] text-sc-ink2">{workspace.memory}</pre>
						{:else}
							<p class="rounded-md border border-sc-line bg-sc-panel px-3 py-4 text-center text-[12px] text-sc-ink3">This agent has not written any long-term memory yet.</p>
						{/if}
						<details class="rounded-md border border-sc-line bg-sc-panel">
							<summary class="cursor-pointer px-3 py-2 text-[12px] text-sc-ink2 hover:text-sc-ink">Today’s log · {workspace.memory_day}{workspace.memory_today.trim() ? '' : ' (empty)'}</summary>
							{#if workspace.memory_today.trim()}
								<pre class="m-0 max-h-[40vh] overflow-auto whitespace-pre-wrap break-words border-t border-sc-line p-3 font-plex-mono text-[11.5px] leading-[1.55] text-sc-ink2">{workspace.memory_today}</pre>
							{/if}
						</details>
						<details class="rounded-md border border-sc-line bg-sc-panel">
							<summary class="cursor-pointer px-3 py-2 text-[12px] text-sc-ink2 hover:text-sc-ink">Activity log · latest {workspace.logs.length}</summary>
							<ol class="m-0 max-h-[40vh] list-none overflow-auto border-t border-sc-line p-0 font-plex-mono text-[11px]">
								{#each workspace.logs as log (log.id)}
									<li class="grid grid-cols-[92px_minmax(0,1fr)] gap-2 px-3 py-1 hover:bg-sc-hover">
										<span class="text-sc-ink3" title={shortDateTime(log.created_at)}>{shortDateTime(log.created_at)}</span>
										<span class={`break-words ${logTone(log.level)}`}>{log.message}</span>
									</li>
								{:else}
									<li class="px-3 py-2 text-sc-ink3">No log lines yet.</li>
								{/each}
							</ol>
						</details>
					{/if}
				</div>
			{:else if tab === 'instructions'}
				<div class="grid gap-3">
					<p class="m-0 text-[12px] text-sc-ink2">
						These documents are what the agent reads at the start of every run, before the task itself. Edits apply from its next run; a run in progress keeps what it started with.
					</p>
					<div class="flex flex-wrap gap-1" role="tablist" aria-label="Documents">
						{#each DOCS as doc (doc.key)}
							<button
								type="button"
								role="tab"
								aria-selected={docKey === doc.key}
								class={`rounded-md border px-2.5 py-1 text-[12px] transition-colors ${docKey === doc.key ? 'border-sc-ink4 bg-sc-raise text-sc-ink' : 'border-sc-line2 text-sc-ink3 hover:text-sc-ink'}`}
								on:click={() => (docKey = doc.key)}
							>{doc.label} <span class="font-plex-mono text-[10.5px] text-sc-ink3">{doc.file}</span>{#if drafts[doc.key] !== saved[doc.key]}<span class="ml-1 text-[#e7b24a]">●</span>{/if}</button>
						{/each}
					</div>
					<p class="m-0 text-[11.5px] text-sc-ink3">{activeDoc.help}{#if !saved[docKey].trim()}{' '}Empty: the agent falls back to its built-in instructions.{/if}</p>
					{#if workspaceError}
						<p class="text-[12px] text-[#f2956f]">{workspaceError}</p>
					{:else if !workspace}
						<p class="text-[12px] text-sc-ink3">Loading documents…</p>
					{:else}
						<textarea
							class="min-h-[46vh] w-full resize-y rounded-md border border-sc-line2 bg-sc-panel p-3 font-plex-mono text-[12px] leading-[1.55] text-sc-ink outline-none focus:border-sc-ink4"
							spellcheck="false"
							aria-label={activeDoc.file}
							bind:value={drafts[docKey]}
						></textarea>
						<div class="flex items-center justify-between gap-2">
							<span class="text-[11px] text-sc-ink3">{drafts[docKey].length.toLocaleString('en-US')} characters</span>
							<div class="flex gap-2">
								<button type="button" class="rounded-md border border-sc-line2 px-3 py-1 text-[12px] text-sc-ink2 hover:text-sc-ink disabled:opacity-40" disabled={drafts[docKey] === saved[docKey] || docSaving} on:click={() => (drafts = { ...drafts, [docKey]: saved[docKey] })}>Revert</button>
								<button type="button" class="rounded-md bg-sc-ink px-3 py-1 text-[12px] font-medium text-black hover:bg-white disabled:opacity-40" disabled={drafts[docKey] === saved[docKey] || docSaving} on:click={saveDoc}>{docSaving ? 'Saving…' : `Save ${activeDoc.file}`}</button>
							</div>
						</div>
					{/if}
				</div>
			{:else if tab === 'tools'}
				<div class="grid gap-4">
					<section class="rounded-md border border-sc-line bg-sc-panel px-3.5 py-3">
						<div class="flex flex-wrap items-baseline justify-between gap-2">
							<h3 class="m-0 text-[12.5px] font-medium text-sc-ink">Built-in tools</h3>
							<a class="text-[12px] text-sc-ink2 hover:text-sc-ink" href={`/agents/toolsets?agent=${encodeURIComponent(agent.id)}`}>Open the tool matrix →</a>
						</div>
						<p class="m-0 mt-1 text-[12px] text-sc-ink3">Which tools this agent may call in each context (task, chat, research), with per-tool overrides.</p>
					</section>
					<section class="rounded-md border border-sc-line bg-sc-panel">
						<div class="flex items-center justify-between gap-2 border-b border-sc-line px-3.5 py-2.5">
							<h3 class="m-0 text-[12.5px] font-medium text-sc-ink">MCP servers</h3>
							<span class="text-[11px] text-sc-ink3">An agent can only call tools from servers granted here</span>
						</div>
						{#if mcpError}
							<p class="m-0 px-3.5 py-3 text-[12px] text-[#f2956f]">{mcpError}</p>
						{:else if mcpLoading && !mcpLoaded}
							<p class="m-0 px-3.5 py-3 text-[12px] text-sc-ink3">Loading servers…</p>
						{:else if servers.length === 0}
							<p class="m-0 px-3.5 py-3 text-[12px] text-sc-ink3">No MCP servers are configured. Add one under <a class="text-sc-ink hover:underline" href="/integrations/mcp">Integrations → MCP</a>.</p>
						{:else}
							<ul class="m-0 list-none divide-y divide-sc-line p-0">
								{#each servers as server (server.name)}
									{@const granted = grantedNames.has(server.name)}
									<li class="flex items-center justify-between gap-3 px-3.5 py-2">
										<div class="min-w-0">
											<div class="font-plex-mono text-[12px] text-sc-ink">{server.name}</div>
											<div class="text-[11px] text-sc-ink3">{server.transport} · {server.enabled ? 'enabled' : 'disabled: no tools register until it is enabled'}</div>
										</div>
										<button
											type="button"
											class={`rounded border px-2 py-0.5 text-[11px] transition-colors disabled:opacity-50 ${granted ? 'border-[#3cc48f]/45 text-[#3cc48f] hover:border-[#e5574f]/60 hover:text-[#f2956f]' : 'border-sc-line2 text-sc-ink2 hover:border-sc-ink hover:text-sc-ink'}`}
											disabled={mcpBusy !== null}
											on:click={() => toggleGrant(server.name, granted)}
										>{mcpBusy === server.name ? 'Working…' : granted ? 'Granted · revoke' : 'Grant'}</button>
									</li>
								{/each}
							</ul>
						{/if}
					</section>
				</div>
			{:else if tab === 'settings'}
				<div class="grid gap-4">
					<section class="grid gap-2 rounded-md border border-sc-line bg-sc-panel px-3.5 py-3">
						<label class="grid gap-1 text-[12px] text-sc-ink2" for="agent-name">Display name</label>
						<div class="flex gap-2">
							<input id="agent-name" class="min-w-0 flex-1 rounded-md border border-sc-line2 bg-sc-bg px-2.5 py-1.5 text-[12.5px] text-sc-ink outline-none placeholder:text-sc-ink4 focus:border-sc-ink4" maxlength="60" bind:value={nameDraft} />
							<button type="button" class="rounded-md border border-sc-line2 px-3 py-1 text-[12px] text-sc-ink2 hover:border-sc-ink hover:text-sc-ink disabled:opacity-40" disabled={nameSaving || !nameDraft.trim() || nameDraft.trim() === agent.name} on:click={saveName}>{nameSaving ? 'Saving…' : 'Rename'}</button>
						</div>
					</section>
					<section class="rounded-md border border-sc-line bg-sc-panel px-3.5 py-3">
						<div class="flex flex-wrap items-baseline justify-between gap-2">
							<h3 class="m-0 text-[12.5px] font-medium text-sc-ink">Model</h3>
							<a class="text-[12px] text-sc-ink2 hover:text-sc-ink" href="/agents?tab=routing">Change model & fallbacks →</a>
						</div>
						<p class="m-0 mt-1 text-[12px] text-sc-ink2"><span class="font-plex-mono text-sc-ink">{model.model}</span> on {model.provider}. Every agent's model and fallbacks are set in one place: Setup → Agent models.</p>
						{#if fallback}
							<p class={`m-0 mt-1.5 text-[12px] ${TONE_TEXT[fallback.tone]}`}>{fallback.text[0].toUpperCase() + fallback.text.slice(1)}. <span class="text-sc-ink3">{fallback.help}</span></p>
						{/if}
					</section>
					{#if canPause(agent)}
						<section class="rounded-md border border-sc-line bg-sc-panel px-3.5 py-3">
							<div class="flex flex-wrap items-center justify-between gap-2">
								<h3 class="m-0 text-[12.5px] font-medium text-sc-ink">{agent.enabled ? 'Taking runs' : 'Paused'}</h3>
								<button type="button" class={`rounded-md border px-3 py-1 text-[12px] disabled:opacity-50 ${agent.enabled ? 'border-sc-line2 text-sc-ink2 hover:border-sc-ink3 hover:text-sc-ink' : 'border-[#7fb2ff]/50 text-[#a9cbff] hover:border-[#7fb2ff]'}`} disabled={toggleBusy} on:click={() => dispatch('toggle', agent)}>{agent.enabled ? 'Pause agent' : 'Resume agent'}</button>
							</div>
							<p class="m-0 mt-1 text-[12px] text-sc-ink3">{job.pauseEffect} A run already in progress finishes.</p>
						</section>
					{/if}
					<section class="grid gap-2 rounded-md border border-sc-line bg-sc-panel px-3.5 py-3">
						<h3 class="m-0 text-[12.5px] font-medium text-sc-ink">Discord bot</h3>
						<p class="m-0 text-[12px] text-sc-ink3">Give this agent its own Discord bot so it can post and answer in its channel. {hasDiscordToken ? 'A token is saved; enter a new one to replace it.' : 'No token saved.'}</p>
						<div class="flex flex-wrap gap-2">
							<input type="password" autocomplete="off" class="min-w-[220px] flex-1 rounded-md border border-sc-line2 bg-sc-bg px-2.5 py-1.5 text-[12.5px] text-sc-ink outline-none placeholder:text-sc-ink4 focus:border-sc-ink4" placeholder={hasDiscordToken ? '•••••••• saved' : 'Bot token'} bind:value={discordToken} />
							<button type="button" class="rounded-md border border-sc-line2 px-3 py-1 text-[12px] text-sc-ink2 hover:border-sc-ink hover:text-sc-ink disabled:opacity-40" disabled={discordSaving || !discordToken.trim()} on:click={saveDiscordToken}>{discordSaving ? 'Saving…' : 'Save token'}</button>
							<button type="button" class="rounded-md border border-sc-line2 px-3 py-1 text-[12px] text-sc-ink2 hover:border-sc-ink hover:text-sc-ink disabled:opacity-40" disabled={discordTesting || (!hasDiscordToken && !discordToken.trim())} on:click={testDiscord}>{discordTesting ? 'Sending…' : 'Send test'}</button>
						</div>
					</section>
					{#if !agent.is_core}
						<section class="rounded-md border border-[#e5574f]/35 bg-[#e5574f]/[0.04] px-3.5 py-3">
							<div class="flex flex-wrap items-center justify-between gap-2">
								<div>
									<h3 class="m-0 text-[12.5px] font-medium text-sc-ink">Delete this agent</h3>
									<p class="m-0 mt-0.5 text-[12px] text-sc-ink3">Its runs are deleted with it; its memory and documents stay on disk.</p>
								</div>
								<button type="button" class="rounded-md border border-[#e5574f]/60 bg-[#e5574f]/10 px-3 py-1 text-[12px] text-[#f6b4ae] hover:bg-[#e5574f]/20 disabled:opacity-50" disabled={deleting} on:click={deleteAgent}>{deleting ? 'Deleting…' : 'Delete agent'}</button>
							</div>
						</section>
					{/if}
				</div>
			{/if}
		</div>

	</div>
</div>
