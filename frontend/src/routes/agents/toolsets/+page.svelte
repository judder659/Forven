<script lang="ts">
	import { onMount } from 'svelte';
	import { page } from '$app/stores';
	import {
		deleteAgentToolsetOverrides,
		getAgentToolsets,
		getForvenAgents,
		type AgentToolsetsResponse,
		type ForvenAgent,
		type ToolDefinition,
		type ToolsetOverrideRule,
	} from '$lib/api/forven';

	// The kinds of work an agent does, in the order shown. Each is a tools
	// context in forven.agents.tool_registry; forven.agents.runner picks it from
	// the task type. 'recovery' is not listed: no task runs in it.
	const JOBS: Array<{ context: string; label: string; help: string }> = [
		{ context: 'develop', label: 'Building strategies', help: 'Idea and strategy-building tasks.' },
		{ context: 'research', label: 'Research', help: 'Research and analysis tasks.' },
		{
			context: 'scheduled',
			label: 'Routine work',
			help: 'Everything else Forven runs on its own: backtests, risk checks, post-mortems, Brain cycles.',
		},
		{ context: 'interactive', label: 'When you ask', help: 'Your chat and tasks you start by hand.' },
	];

	type ToolRow = { tool: ToolDefinition; summary: string; jobs: Record<string, boolean>; everywhere: boolean };

	let agents: ForvenAgent[] = [];
	let agentsLoading = true;
	let agentsError: string | null = null;
	let selectedAgentId = '';
	let toolsets: AgentToolsetsResponse | null = null;
	let loading = false;
	let clearing = false;
	let error: string | null = null;
	let actionMessage: string | null = null;
	let nameFilter = '';

	$: rows = toolsets ? buildRows(toolsets) : [];
	$: filteredRows = rows.filter((row) => {
		const needle = nameFilter.trim().toLowerCase();
		return !needle || row.tool.name.toLowerCase().includes(needle) || row.summary.toLowerCase().includes(needle);
	});
	$: savedOverrides = toolsets ? collectOverrides(toolsets) : [];
	$: selectedAgent = agents.find((a) => String(a.id) === selectedAgentId);

	function firstSentence(text: string | undefined): string {
		const clean = String(text || '').trim();
		// A sentence ends at . ! ? before a capital letter or the end, so 'e.g. orb' doesn't.
		const match = clean.match(/^.*?[.!?](?=\s+[A-Z]|$)/);
		return (match ? match[0] : clean).trim();
	}

	function buildRows(data: AgentToolsetsResponse): ToolRow[] {
		const out: ToolRow[] = [];
		for (const tool of data.all_tools) {
			const jobs: Record<string, boolean> = {};
			let available = false;
			for (const job of JOBS) {
				const entry = data.contexts[job.context]?.effective.find((e) => e.name === tool.name);
				// No entry: the tool is outside this agent's permissions.
				if (entry) available = true;
				jobs[job.context] = Boolean(entry?.enabled);
			}
			if (!available) continue;
			out.push({
				tool,
				summary: firstSentence(tool.description),
				jobs,
				everywhere: JOBS.every((job) => jobs[job.context]),
			});
		}
		// Tools usable everywhere first, then the rest; alphabetical within each.
		return out.sort((a, b) =>
			a.everywhere === b.everywhere ? a.tool.name.localeCompare(b.tool.name) : a.everywhere ? -1 : 1,
		);
	}

	function collectOverrides(data: AgentToolsetsResponse): Array<ToolsetOverrideRule & { context: string }> {
		const out: Array<ToolsetOverrideRule & { context: string }> = [];
		for (const [context, value] of Object.entries(data.contexts)) {
			for (const rule of value.overrides) out.push({ ...rule, context });
		}
		return out;
	}

	function jobLabel(context: string): string {
		return JOBS.find((job) => job.context === context)?.label ?? context;
	}

	async function loadAgents() {
		agentsLoading = true;
		agentsError = null;
		try {
			const list = await getForvenAgents();
			agents = (list || []).filter((a) => !!a.id);
			const requested = $page.url.searchParams.get('agent');
			const preselect =
				requested && agents.some((a) => String(a.id) === requested)
					? requested
					: agents.length > 0
						? String(agents[0].id)
						: '';
			if (!selectedAgentId && preselect) {
				selectedAgentId = preselect;
				await loadToolsets(selectedAgentId);
			}
		} catch (err) {
			agentsError = err instanceof Error ? err.message : String(err);
		} finally {
			agentsLoading = false;
		}
	}

	async function loadToolsets(agentId: string) {
		if (!agentId) return;
		loading = true;
		error = null;
		try {
			toolsets = await getAgentToolsets(agentId);
		} catch (err) {
			error = err instanceof Error ? err.message : String(err);
			toolsets = null;
		} finally {
			loading = false;
		}
	}

	async function handleSelectAgent(id: string) {
		if (id === selectedAgentId) return;
		selectedAgentId = id;
		actionMessage = null;
		await loadToolsets(id);
	}

	async function clearOverrides() {
		if (!selectedAgentId || savedOverrides.length === 0) return;
		clearing = true;
		error = null;
		actionMessage = null;
		try {
			const contexts = [...new Set(savedOverrides.map((rule) => rule.context))];
			let deleted = 0;
			for (const context of contexts) {
				deleted += (await deleteAgentToolsetOverrides(selectedAgentId, context)).deleted;
			}
			actionMessage = `Removed ${deleted} saved exception${deleted === 1 ? '' : 's'}. This agent now uses the standard rules.`;
			await loadToolsets(selectedAgentId);
		} catch (err) {
			error = err instanceof Error ? err.message : String(err);
		} finally {
			clearing = false;
		}
	}

	onMount(loadAgents);
</script>

<svelte:head><title>What agents can use | Forven</title></svelte:head>

<div class="flex h-full min-h-0 overflow-hidden">
	<aside class="w-64 border-r border-sc-line bg-sc-panel overflow-y-auto">
		<header class="px-4 py-4 border-b border-sc-line">
			<a href={selectedAgentId ? `/agents?agent=${encodeURIComponent(selectedAgentId)}` : '/agents'} class="text-[12px] text-sc-ink3 hover:text-sc-ink">← Agents</a>
			<h1 class="text-[22px] font-semibold tracking-[-0.01em] text-sc-ink mt-1">What agents can use</h1>
		</header>
		{#if agentsLoading}
			<div class="px-4 py-3 text-xs text-sc-ink3">Loading agents...</div>
		{:else if agentsError}
			<div class="px-4 py-3 text-xs text-red-400">{agentsError}</div>
		{:else if agents.length === 0}
			<div class="px-4 py-3 text-xs text-sc-ink3">No agents.</div>
		{:else}
			<ul class="divide-y divide-sc-line">
				{#each agents as agent}
					<li>
						<button
							type="button"
							disabled={loading || clearing}
							class="w-full text-left px-4 py-2 hover:bg-sc-panel2 transition-colors disabled:opacity-50 disabled:cursor-not-allowed {selectedAgentId === agent.id ? 'bg-sc-panel2 text-sc-ink border-l-2 border-l-sc-ink pl-[14px]' : 'text-sc-ink2'}"
							on:click={() => void handleSelectAgent(String(agent.id))}
						>
							<div class="text-sm">{agent.name || agent.id}</div>
							<div class="text-[11px] text-sc-ink3 font-mono">{agent.id}</div>
						</button>
					</li>
				{/each}
			</ul>
		{/if}
	</aside>

	<section class="flex-1 overflow-y-auto p-6 space-y-4">
		{#if !selectedAgentId}
			<div class="text-sc-ink3">Select an agent to see its tools.</div>
		{:else if loading}
			<div class="text-sc-ink3">Loading tools for {selectedAgent?.name || selectedAgentId}...</div>
		{:else if error}
			<div class="border border-red-900 bg-red-500/5 text-red-400 text-xs px-3 py-2">{error}</div>
		{:else if !toolsets}
			<div class="text-sc-ink3">No tool data.</div>
		{:else}
			<header class="space-y-2">
				<h2 class="text-[16px] font-semibold text-sc-ink">{selectedAgent?.name || toolsets.agent_id}</h2>
				<p class="text-xs text-sc-ink3 max-w-3xl">
					{rows.length} tools. Forven's safety rules decide which ones this agent can use, based on the job it is
					doing. Nothing that runs on its own can wipe your data.
				</p>
				<dl class="grid gap-x-6 gap-y-1 text-[11px] text-sc-ink3 sm:grid-cols-2 max-w-3xl">
					{#each JOBS as job}
						<div><dt class="inline text-sc-ink2">{job.label}:</dt> <dd class="inline">{job.help}</dd></div>
					{/each}
				</dl>
			</header>

			{#if actionMessage}<div class="border border-emerald-900 bg-emerald-500/5 text-emerald-400 text-xs px-3 py-2">{actionMessage}</div>{/if}

			{#if savedOverrides.length > 0}
				<div class="border border-yellow-900 bg-yellow-500/5 px-3 py-2 text-xs text-sc-ink2 space-y-1.5">
					<div class="flex flex-wrap items-center justify-between gap-2">
						<span class="text-yellow-400">This agent has {savedOverrides.length} saved exception{savedOverrides.length === 1 ? '' : 's'} to the standard rules. They still apply.</span>
						<button type="button" class="terminal-button text-[12px] disabled:opacity-40" disabled={clearing} on:click={() => void clearOverrides()}>
							{clearing ? 'Removing...' : 'Remove exceptions'}
						</button>
					</div>
					<ul class="space-y-0.5">
						{#each savedOverrides as rule}
							<li><span class="font-mono">{rule.tool_name}</span>: {rule.enabled ? 'allowed' : 'blocked'} in {jobLabel(rule.context)}</li>
						{/each}
					</ul>
				</div>
			{/if}

			<div class="flex items-center gap-2 text-xs">
				<input type="text" id="tool-filter" bind:value={nameFilter} placeholder="Filter tools..." class="terminal-input w-56 !py-1" />
				<span class="text-sc-ink3">{filteredRows.length} shown</span>
			</div>

			<div class="overflow-x-auto">
				<table class="w-full text-xs">
					<thead class="text-[10px] text-sc-ink3 uppercase tracking-wider">
						<tr>
							<th class="text-left px-3 py-2">Tool</th>
							{#each JOBS as job}
								<th class="text-center px-2 py-2 whitespace-nowrap" title={job.help}>{job.label}</th>
							{/each}
						</tr>
					</thead>
					<tbody>
						{#each filteredRows as row}
							<tr class="border-t border-sc-line">
								<td class="px-3 py-1.5">
									<div class="font-mono text-sc-ink">{row.tool.name}</div>
									{#if row.summary}<div class="text-[11px] text-sc-ink3">{row.summary}</div>{/if}
								</td>
								{#each JOBS as job}
									<td class="px-2 py-1.5 text-center">
										{#if row.jobs[job.context]}
											<span class="text-emerald-400" aria-label="Can use">✓</span>
										{:else}
											<span class="text-sc-ink4" aria-label="Can't use">–</span>
										{/if}
									</td>
								{/each}
							</tr>
						{/each}
					</tbody>
				</table>
			</div>
		{/if}
	</section>
</div>
