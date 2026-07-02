<script lang="ts">
	import { onMount } from 'svelte';
	import { page } from '$app/stores';
	import {
		deleteAgentToolsetOverrides,
		getAgentToolsets,
		getForvenAgents,
		putAgentToolsetOverrides,
		type AgentToolsetsResponse,
		type ForvenAgent,
		type ToolDefinition,
		type ToolsetEffectiveEntry,
		type ToolsetOverrideRule,
	} from '$lib/api/forven';

	type RowState = 'default' | 'enable' | 'disable';

	let agents: ForvenAgent[] = [];
	let agentsLoading = true;
	let agentsError: string | null = null;
	let selectedAgentId = '';
	let toolsets: AgentToolsetsResponse | null = null;
	let loading = false;
	let saving = false;
	let error: string | null = null;
	let actionMessage: string | null = null;
	let activeContext = '';
	let categoryFilter: string | 'all' = 'all';
	let nameFilter = '';
	let overrideMap: Map<string, RowState> = new Map();
	let savedOverrideMap: Map<string, RowState> = new Map();
	let dirty = false;

	$: contexts = toolsets ? toolsets.valid_contexts : [];
	$: categories = toolsets ? ['all', ...toolsets.categories] : ['all'];
	$: tools = toolsets ? toolsets.all_tools : ([] as ToolDefinition[]);
	$: filteredTools = tools.filter(
		(t) =>
			(categoryFilter === 'all' || t.category === categoryFilter) &&
			(!nameFilter.trim() || t.name.toLowerCase().includes(nameFilter.trim().toLowerCase())),
	);
	$: activeContextData = toolsets && activeContext ? toolsets.contexts[activeContext] : null;
	$: effectiveByName = buildEffectiveLookup(activeContextData?.effective ?? []);
	$: contextOverrideCount = activeContextData ? activeContextData.overrides.length : 0;
	$: filteredEnabledCount = filteredTools.filter((t) => effectiveByName.get(t.name)?.enabled).length;
	$: filteredDisabledCount = filteredTools.length - filteredEnabledCount;

	function contextOverrideCountFor(ctx: string): number {
		return toolsets?.contexts[ctx]?.overrides.length ?? 0;
	}

	function buildEffectiveLookup(effective: ToolsetEffectiveEntry[]): Map<string, ToolsetEffectiveEntry> {
		const map = new Map<string, ToolsetEffectiveEntry>();
		for (const entry of effective) map.set(entry.name, entry);
		return map;
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
		dirty = false;
		try {
			toolsets = await getAgentToolsets(agentId);
			if (!activeContext || !toolsets.valid_contexts.includes(activeContext)) {
				activeContext = toolsets.valid_contexts[0] || '';
			}
			rebuildOverrideMap();
		} catch (err) {
			error = err instanceof Error ? err.message : String(err);
			toolsets = null;
		} finally {
			loading = false;
		}
	}

	function rebuildOverrideMap() {
		const map = new Map<string, RowState>();
		const ctxData = activeContextData;
		if (ctxData) {
			for (const rule of ctxData.overrides) {
				map.set(rule.tool_name, rule.enabled ? 'enable' : 'disable');
			}
		}
		overrideMap = map;
		savedOverrideMap = new Map(map);
		dirty = false;
	}

	function rowState(toolName: string): RowState {
		return overrideMap.get(toolName) || 'default';
	}

	function isRowDirty(toolName: string): boolean {
		return (overrideMap.get(toolName) || 'default') !== (savedOverrideMap.get(toolName) || 'default');
	}

	function cycleRow(toolName: string) {
		const current = rowState(toolName);
		const next: RowState = current === 'default' ? 'enable' : current === 'enable' ? 'disable' : 'default';
		const map = new Map(overrideMap);
		if (next === 'default') map.delete(toolName);
		else map.set(toolName, next);
		overrideMap = map;
		dirty = true;
	}

	function confirmDiscard(): boolean {
		if (!dirty) return true;
		return window.confirm('Discard unsaved override changes?');
	}

	async function handleSelectAgent(id: string) {
		if (id === selectedAgentId) return;
		if (!confirmDiscard()) return;
		selectedAgentId = id;
		await loadToolsets(id);
	}

	async function handleSelectContext(ctx: string) {
		if (ctx === activeContext) return;
		if (!confirmDiscard()) return;
		activeContext = ctx;
		rebuildOverrideMap();
	}

	function effectiveLabel(tool: ToolDefinition): string {
		const eff = effectiveByName.get(tool.name);
		if (!eff) return 'unknown';
		return `${eff.enabled ? 'enabled' : 'disabled'} via ${eff.source}`;
	}

	function effectiveClass(tool: ToolDefinition): string {
		const eff = effectiveByName.get(tool.name);
		if (!eff) return 'text-[#888]';
		return eff.enabled ? 'text-emerald-400' : 'text-red-400';
	}

	function rowChipClass(state: RowState): string {
		switch (state) {
			case 'enable':
				return 'border-emerald-900 bg-emerald-500/10 text-emerald-400';
			case 'disable':
				return 'border-red-900 bg-red-500/10 text-red-400';
			default:
				return 'border-[#333] text-[#888]';
		}
	}

	function rowChipLabel(state: RowState): string {
		switch (state) {
			case 'enable':
				return 'Override: enable';
			case 'disable':
				return 'Override: disable';
			default:
				return 'Use default';
		}
	}

	async function saveOverrides() {
		if (!selectedAgentId || !activeContext) return;
		saving = true;
		error = null;
		actionMessage = null;
		try {
			const overrides: Array<{ tool_name: string; enabled: boolean }> = [];
			for (const [name, state] of overrideMap.entries()) {
				if (state === 'default') continue;
				overrides.push({ tool_name: name, enabled: state === 'enable' });
			}
			await putAgentToolsetOverrides(selectedAgentId, activeContext, overrides);
			actionMessage = `Saved ${overrides.length} override(s) for ${activeContext}.`;
			await loadToolsets(selectedAgentId);
		} catch (err) {
			error = err instanceof Error ? err.message : String(err);
		} finally {
			saving = false;
		}
	}

	async function resetContext() {
		if (!selectedAgentId || !activeContext) return;
		if (!window.confirm(`Reset all overrides for ${activeContext}? This cannot be undone.`)) return;
		saving = true;
		error = null;
		actionMessage = null;
		try {
			const res = await deleteAgentToolsetOverrides(selectedAgentId, activeContext);
			actionMessage = `Reset ${res.deleted} override(s) for ${activeContext}.`;
			await loadToolsets(selectedAgentId);
		} catch (err) {
			error = err instanceof Error ? err.message : String(err);
		} finally {
			saving = false;
		}
	}

	onMount(loadAgents);
</script>

<svelte:head><title>Agent Toolsets | Forven</title></svelte:head>

<div class="flex h-screen overflow-hidden">
	<aside class="w-64 border-r border-[#222] bg-[#050505] overflow-y-auto">
		<header class="px-4 py-4 border-b border-[#222]">
			<div class="text-[10px] uppercase tracking-wider text-[#666]">Agents</div>
			<h1 class="text-sm font-bold uppercase tracking-widest text-white mt-1">Toolset matrix</h1>
		</header>
		{#if agentsLoading}
			<div class="px-4 py-3 text-xs text-[#666]">Loading agents...</div>
		{:else if agentsError}
			<div class="px-4 py-3 text-xs text-red-400">{agentsError}</div>
		{:else if agents.length === 0}
			<div class="px-4 py-3 text-xs text-[#666]">No agents.</div>
		{:else}
			<ul class="divide-y divide-[#1a1a1a]">
				{#each agents as agent}
					<li>
						<button
							type="button"
							disabled={loading || saving}
							class="w-full text-left px-4 py-2 hover:bg-[#111] transition-colors disabled:opacity-50 disabled:cursor-not-allowed {selectedAgentId === agent.id ? 'bg-[#111] text-white border-l-2 border-l-white pl-[14px]' : 'text-[#888]'}"
							on:click={() => void handleSelectAgent(String(agent.id))}
						>
							<div class="text-sm font-mono">{agent.id}</div>
							<div class="text-[11px] text-[#666]">{agent.name || agent.role || ''}</div>
						</button>
					</li>
				{/each}
			</ul>
		{/if}
	</aside>

	<section class="flex-1 overflow-y-auto p-6 space-y-4">
		{#if !selectedAgentId}
			<div class="text-[#666]">Select an agent to view its toolset.</div>
		{:else if loading}
			<div class="text-[#666]">Loading toolset for {selectedAgentId}...</div>
		{:else if error}
			<div class="border border-red-900 bg-red-500/5 text-red-400 text-xs px-3 py-2">{error}</div>
		{:else if !toolsets}
			<div class="text-[#666]">No toolset data.</div>
		{:else}
			<header class="space-y-2">
				<div class="flex items-end justify-between gap-3">
					<div>
						<div class="text-[10px] uppercase tracking-wider text-[#666]">Agent</div>
						<h2 class="text-lg font-bold uppercase tracking-widest text-white">{toolsets.agent_id}</h2>
					</div>
					<div class="flex items-center gap-2">
						<button
							type="button"
							disabled={saving || !dirty}
							class="terminal-button-primary text-xs disabled:opacity-40"
							on:click={() => void saveOverrides()}
						>
							{saving ? 'Saving...' : 'Save changes'}
						</button>
						<button
							type="button"
							disabled={saving || contextOverrideCount === 0}
							class="terminal-button text-xs disabled:opacity-40"
							on:click={() => void resetContext()}
						>
							Reset context
						</button>
					</div>
				</div>
				<p class="text-xs text-[#666] max-w-3xl">
					Per-context overrides for this agent. Resolution order:
					exact tool name &gt; <code>mcp:&lt;server&gt;</code> &gt; <code>mcp:*</code> &gt;
					<code>category:&lt;cat&gt;</code> &gt; default-deny set. Click a row's chip to cycle
					between <em>default</em>, <em>override: enable</em>, and <em>override: disable</em>.
				</p>
			</header>

			{#if actionMessage}<div class="border border-emerald-900 bg-emerald-500/5 text-emerald-400 text-xs px-3 py-2">{actionMessage}</div>{/if}

			<nav class="flex flex-wrap items-center gap-2 border-b border-[#222] pb-2">
				{#each contexts as ctx}
					{@const ctxOverrides = contextOverrideCountFor(ctx)}
					<button
						type="button"
						disabled={loading || saving}
						class="flex items-center gap-1.5 text-xs uppercase tracking-wider px-3 py-1.5 border transition-colors disabled:opacity-50 disabled:cursor-not-allowed {activeContext === ctx ? 'border-[#555] bg-[#111] text-white' : 'border-[#333] text-[#888] hover:text-white'}"
						on:click={() => void handleSelectContext(ctx)}
					>
						{ctx}
						{#if ctxOverrides > 0}
							<span class="text-[10px] px-1 border border-yellow-900 bg-yellow-500/10 text-yellow-400">{ctxOverrides}</span>
						{/if}
					</button>
				{/each}
				<div class="ml-auto flex items-center gap-2 text-xs">
					<input
						type="text"
						bind:value={nameFilter}
						placeholder="Filter tools..."
						class="terminal-input w-40 !py-1"
					/>
					<span class="text-[#666] uppercase tracking-wider">Category</span>
					<select bind:value={categoryFilter} class="terminal-select !w-auto !py-1">
						{#each categories as cat}
							<option value={cat}>{cat}</option>
						{/each}
					</select>
				</div>
			</nav>

			<div class="flex items-center gap-3 text-[11px] text-[#666]">
				<span>{filteredTools.length} tool(s)</span>
				<span class="text-emerald-400">{filteredEnabledCount} enabled</span>
				<span class="text-red-400">{filteredDisabledCount} disabled</span>
			</div>

			<table class="w-full text-xs">
				<thead class="text-[10px] text-[#666] uppercase tracking-wider">
					<tr>
						<th class="text-left px-3 py-2">Tool</th>
						<th class="text-left px-3 py-2">Category</th>
						<th class="text-left px-3 py-2">Effective</th>
						<th class="text-left px-3 py-2">Override</th>
					</tr>
				</thead>
				<tbody>
					{#each filteredTools as tool}
						{@const state = rowState(tool.name)}
						{@const rowDirty = isRowDirty(tool.name)}
						<tr class="border-t border-[#1a1a1a] hover:bg-[#111] transition-colors {rowDirty ? 'bg-yellow-500/5' : ''}">
							<td class="px-3 py-1.5 font-mono text-[#ccc]" title={tool.description || ''}>
								<span class="inline-flex items-center gap-1.5">
									{#if rowDirty}<span class="text-yellow-400" title="Unsaved change">●</span>{/if}
									{tool.name}
									{#if tool.description}<span class="text-[#555]" title={tool.description}>(?)</span>{/if}
								</span>
							</td>
							<td class="px-3 py-1.5 text-[#666]">{tool.category}</td>
							<td class="px-3 py-1.5 {effectiveClass(tool)}">{effectiveLabel(tool)}</td>
							<td class="px-3 py-1.5">
								<button
									type="button"
									disabled={saving}
									class="text-[10px] uppercase tracking-wider px-2 py-0.5 border disabled:opacity-50 {rowChipClass(state)}"
									on:click={() => cycleRow(tool.name)}
								>
									{rowChipLabel(state)}
								</button>
							</td>
						</tr>
					{/each}
				</tbody>
			</table>
		{/if}
	</section>
</div>
