<script lang="ts">
	import { createEventDispatcher, onDestroy, onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { page } from '$app/stores';
	import type { TaskContainer } from '$lib/api';
	import {
		dismissAgentRun,
		forEachRun,
		listAgentRuns,
		resumeAgentRun,
		type AgentFleet,
	} from '$lib/api/agentsHub';
	import { addToast } from '$lib/stores/processTracker';
	import { createRealtimeRefresh } from '$lib/utils/realtime';
	import { typeLabel } from '$lib/utils/agentsHub/agents';
	import { fmtCost, fmtSeconds, fmtTokens, plural } from '$lib/utils/agentsHub/format';
	import { TONE_PILL, type Tone } from '$lib/utils/forge/status';
	import { ago, parseUtc, shortDateTime } from '$lib/utils/forge/time';

	export let fleet: AgentFleet | null = null;
	export let now = Date.now();

	const dispatch = createEventDispatcher<{ changed: void }>();

	type Bucket = 'attention' | 'blocked' | 'failed' | 'running' | 'pending' | 'done' | 'stopped' | 'all';
	const BUCKETS: Array<{ id: Bucket; label: string; statuses: string[] }> = [
		{ id: 'attention', label: 'Needs a look', statuses: ['blocked', 'failed', 'error'] },
		{ id: 'blocked', label: 'Blocked', statuses: ['blocked'] },
		{ id: 'failed', label: 'Failed', statuses: ['failed', 'error'] },
		{ id: 'running', label: 'Running', statuses: ['running'] },
		{ id: 'pending', label: 'Queued', statuses: ['pending', 'paused_manual'] },
		{ id: 'done', label: 'Done', statuses: ['done', 'completed', 'reviewed'] },
		{ id: 'stopped', label: 'Cancelled', statuses: ['cancelled', 'rejected'] },
		{ id: 'all', label: 'All', statuses: [] },
	];
	// Old deep links (?status=failed from the dashboard, the Brain page, the roster).
	const STATUS_ALIASES: Record<string, Bucket> = { error: 'failed', paused_manual: 'pending', reviewed: 'done', cancelled: 'stopped', rejected: 'stopped' };
	const DISMISSIBLE = new Set(['failed', 'error', 'cancelled', 'blocked', 'rejected']);

	let bucket: Bucket = 'attention';
	let agentFilter = '';
	let search = '';
	let rows: TaskContainer[] = [];
	let loading = true;
	let error: string | null = null;
	let selected = new Set<number>();
	let inspect: TaskContainer | null = null;
	let busy: string | null = null;

	function readUrl() {
		const params = $page.url.searchParams;
		const status = (params.get('status') || '').trim().toLowerCase();
		const fromStatus = (BUCKETS.find((item) => item.id === status)?.id ?? STATUS_ALIASES[status]) as Bucket | undefined;
		if (fromStatus) bucket = fromStatus;
		else if (!status) bucket = 'attention';
		agentFilter = (params.get('agent') || '').trim();
		const model = (params.get('model') || '').trim();
		const role = (params.get('role') || '').trim();
		if (model || role) search = model || role;
	}

	function syncUrl() {
		const url = new URL($page.url);
		url.searchParams.set('tab', 'tasks');
		if (bucket === 'attention') url.searchParams.delete('status');
		else url.searchParams.set('status', bucket);
		if (agentFilter) url.searchParams.set('agent', agentFilter);
		else url.searchParams.delete('agent');
		url.searchParams.delete('model');
		url.searchParams.delete('role');
		void goto(`${url.pathname}${url.search}`, { replaceState: true, keepFocus: true, noScroll: true });
	}

	async function load() {
		const spec = BUCKETS.find((item) => item.id === bucket) ?? BUCKETS[0];
		error = null;
		try {
			rows = await listAgentRuns({ statuses: spec.statuses, agentId: agentFilter || undefined, limit: 400 });
			const ids = new Set(rows.map((row) => row.id));
			selected = new Set([...selected].filter((id) => ids.has(id)));
			if (inspect) inspect = rows.find((row) => row.id === inspect?.id) ?? null;
		} catch (err) {
			error = err instanceof Error ? err.message : 'Could not load runs.';
		} finally {
			loading = false;
		}
	}

	function pick(next: Bucket) {
		if (next === bucket) return;
		bucket = next;
		selected = new Set();
		loading = true;
		syncUrl();
		void load();
	}

	function pickAgent(next: string) {
		agentFilter = next;
		selected = new Set();
		loading = true;
		syncUrl();
		void load();
	}

	const realtime = createRealtimeRefresh(load, {
		fallbackMs: 20_000,
		wsDebounceMs: 1500,
		wsEvents: ['task_queued', 'task_status_changed', 'task_completed', 'task_failed'],
	});

	onMount(() => {
		readUrl();
		void load();
		realtime.start();
	});
	onDestroy(() => realtime.stop());

	/** Totals from the fleet summary (server-side, dismissed runs excluded); none for open-ended buckets. */
	function count(id: Bucket, source: AgentFleet | null, agentId: string): number | null {
		if (!source) return null;
		const agents = agentId ? source.agents.filter((item) => item.id === agentId) : source.agents;
		const sum = (pick: (agent: (typeof agents)[number]) => number) => agents.reduce((total, agent) => total + pick(agent), 0);
		if (id === 'blocked') return sum((agent) => agent.blocked);
		if (id === 'failed') return sum((agent) => agent.failed_open);
		if (id === 'attention') return sum((agent) => agent.blocked + agent.failed_open);
		// Brain cycles live in their own queue and are not agent runs here.
		if (id === 'running') return sum((agent) => agent.running.filter((run) => run.type !== 'brain_invoke').length);
		return null;
	}

	function tone(status: string): Tone {
		const value = status.toLowerCase();
		if (value === 'done' || value === 'completed' || value === 'reviewed') return 'ok';
		if (value === 'failed' || value === 'error') return 'fail';
		if (value === 'blocked') return 'caution';
		if (value === 'running') return 'info';
		if (value === 'pending' || value === 'paused_manual') return 'wait';
		return 'idle';
	}

	function seconds(row: TaskContainer, clock: number): number | null {
		const start = parseUtc(row.started_at);
		const end = parseUtc(row.completed_at) ?? (String(row.status) === 'running' ? clock : null);
		return start !== null && end !== null && end >= start ? (end - start) / 1000 : null;
	}

	function when(row: TaskContainer): unknown {
		return row.completed_at ?? row.started_at ?? row.created_at;
	}

	$: names = Object.fromEntries((fleet?.agents ?? []).map((agent) => [agent.id, agent.name]));
	$: query = search.trim().toLowerCase();
	$: shown = query
		? rows.filter((row) =>
				[row.display_id, row.title, row.agent_id, row.strategy_id, row.strategy_display_id, row.model_id, row.provider, row.type, row.error, row.status]
					.map((value) => String(value ?? '').toLowerCase())
					.join(' ')
					.includes(query),
			)
		: rows;
	$: selectedRows = shown.filter((row) => selected.has(row.id));
	$: resumableSelected = selectedRows.filter((row) => row.status === 'blocked').map((row) => row.id);
	$: dismissibleSelected = selectedRows.filter((row) => DISMISSIBLE.has(String(row.status))).map((row) => row.id);
	$: allShownSelected = shown.length > 0 && shown.every((row) => selected.has(row.id));

	function toggleRow(id: number) {
		const next = new Set(selected);
		if (next.has(id)) next.delete(id);
		else next.add(id);
		selected = next;
	}

	function toggleAll() {
		selected = allShownSelected ? new Set() : new Set(shown.map((row) => row.id));
	}

	async function run(kind: 'resume' | 'dismiss', ids: number[]) {
		if (ids.length === 0) return;
		const verb = kind === 'resume' ? 'Resume' : 'Dismiss';
		if (ids.length > 1 && !confirm(`${verb} ${plural(ids.length, 'run')}?${kind === 'resume' ? ' Each continues from its saved checkpoint and uses model spend.' : ' They leave the attention list; the history keeps them.'}`)) return;
		busy = kind;
		const result = await forEachRun(ids, (id) => (kind === 'resume' ? resumeAgentRun(id) : dismissAgentRun(id)));
		busy = null;
		if (result.ok > 0) addToast(`${kind === 'resume' ? 'Resumed' : 'Dismissed'} ${plural(result.ok, 'run')}.`, 'success');
		if (result.failed.length > 0) {
			addToast(`${plural(result.failed.length, 'run')} could not be ${kind === 'resume' ? 'resumed' : 'dismissed'}: ${result.failed[0].error}`, 'error', undefined, 9000);
		}
		selected = new Set();
		await load();
		dispatch('changed');
	}
</script>

<div class="grid gap-3" data-testid="agents-runs">
	<div class="flex flex-wrap items-center gap-2">
		<div class="flex flex-wrap items-center gap-1" role="group" aria-label="Run status">
			{#each BUCKETS as item (item.id)}
				{@const n = count(item.id, fleet, agentFilter)}
				<button
					type="button"
					aria-pressed={bucket === item.id}
					class={`rounded-md border px-2.5 py-1 text-[12px] transition-colors ${bucket === item.id ? 'border-sc-ink4 bg-sc-raise text-sc-ink' : 'border-transparent text-sc-ink3 hover:text-sc-ink'}`}
					on:click={() => pick(item.id)}
				>{item.label}{#if n !== null}<span class="ml-1.5 font-plex-mono text-[11px] text-sc-ink3">{n.toLocaleString('en-US')}</span>{/if}</button>
			{/each}
		</div>
		<div class="ml-auto flex flex-wrap items-center gap-2">
			<select class="rounded-md border border-sc-line2 bg-sc-panel px-2 py-1 text-[12px] text-sc-ink" value={agentFilter} on:change={(event) => pickAgent(event.currentTarget.value)} aria-label="Agent">
				<option value="">All agents</option>
				{#each fleet?.agents ?? [] as agent (agent.id)}
					<option value={agent.id}>{agent.name}</option>
				{/each}
			</select>
			<input type="search" class="w-56 rounded-md border border-sc-line2 bg-sc-panel px-2.5 py-1 text-[12px] text-sc-ink outline-none placeholder:text-sc-ink4 focus:border-sc-ink4" placeholder="Search title, strategy, model, error…" bind:value={search} aria-label="Search runs" />
		</div>
	</div>

	{#if selectedRows.length > 0}
		<div class="sticky top-0 z-10 flex flex-wrap items-center gap-2 rounded-md border border-sc-line2 bg-sc-panel2 px-3 py-2 text-[12px]" data-testid="agents-runs-bulk">
			<span class="text-sc-ink">{plural(selectedRows.length, 'run')} selected</span>
			<button type="button" class="rounded border border-[#7fb2ff]/45 px-2 py-0.5 text-[11.5px] text-[#a9cbff] hover:border-[#7fb2ff] disabled:opacity-40" disabled={busy !== null || resumableSelected.length === 0} on:click={() => run('resume', resumableSelected)}>{busy === 'resume' ? 'Resuming…' : `Resume ${resumableSelected.length}`}</button>
			<button type="button" class="rounded border border-sc-line2 px-2 py-0.5 text-[11.5px] text-sc-ink2 hover:border-sc-ink3 hover:text-sc-ink disabled:opacity-40" disabled={busy !== null || dismissibleSelected.length === 0} on:click={() => run('dismiss', dismissibleSelected)}>{busy === 'dismiss' ? 'Dismissing…' : `Dismiss ${dismissibleSelected.length}`}</button>
			<button type="button" class="ml-auto text-[11.5px] text-sc-ink3 hover:text-sc-ink" on:click={() => (selected = new Set())}>Clear</button>
		</div>
	{/if}

	{#if error}
		<p class="m-0 rounded-md border border-[#e5574f]/40 bg-[#e5574f]/10 px-3 py-2 text-[12px] text-[#f2956f]">{error}</p>
	{/if}

	<div class="grid gap-3 xl:grid-cols-[minmax(0,1fr)_360px]">
		<div class="overflow-x-auto rounded-md border border-sc-line bg-sc-panel">
			<table class="w-full min-w-[860px] border-collapse text-left text-[12px]">
				<thead>
					<tr class="border-b border-sc-line text-[11px] text-sc-ink3">
						<th class="w-8 px-3 py-2"><input type="checkbox" class="accent-[#7fb2ff]" checked={allShownSelected} on:change={toggleAll} aria-label="Select all shown runs" /></th>
						<th class="px-2 py-2 font-medium">Run</th>
						<th class="px-2 py-2 font-medium">Agent</th>
						<th class="px-2 py-2 font-medium">What</th>
						<th class="px-2 py-2 font-medium">Status</th>
						<th class="px-2 py-2 font-medium">When</th>
						<th class="px-2 py-2 text-right font-medium">Took</th>
						<th class="px-2 py-2 text-right font-medium">Cost</th>
						<th class="px-3 py-2 text-right font-medium"><span class="sr-only">Actions</span></th>
					</tr>
				</thead>
				<tbody class="divide-y divide-sc-line">
					{#if loading && rows.length === 0}
						{#each [0, 1, 2, 3, 4, 5] as i (i)}
							<tr><td colspan="9" class="px-3 py-2"><div class="h-6 animate-pulse rounded bg-sc-raise/60"></div></td></tr>
						{/each}
					{:else if shown.length === 0}
						<tr><td colspan="9" class="px-3 py-8 text-center text-sc-ink3">{bucket === 'attention' ? 'Nothing blocked or failed. Nothing needs a look.' : 'No runs match this view.'}</td></tr>
					{:else}
						{#each shown as row (row.id)}
							{@const status = String(row.status)}
							{@const took = seconds(row, now)}
							<tr class={`cursor-pointer align-top hover:bg-sc-hover ${inspect?.id === row.id ? 'bg-sc-raise/60' : ''}`} on:click={() => (inspect = row)} data-testid="agents-run-row">
								<td class="px-3 py-2" on:click|stopPropagation><input type="checkbox" class="accent-[#7fb2ff]" checked={selected.has(row.id)} on:change={() => toggleRow(row.id)} aria-label={`Select ${row.display_id}`} /></td>
								<td class="whitespace-nowrap px-2 py-2 font-plex-mono">
									{#if row.display_id}
										<a class="text-sc-ink hover:underline" href={`/tasks/${encodeURIComponent(row.display_id)}?returnTo=${encodeURIComponent('/agents?tab=tasks')}`} on:click|stopPropagation>{row.display_id}</a>
									{:else}<span class="text-sc-ink3">#{row.id}</span>{/if}
								</td>
								<td class="whitespace-nowrap px-2 py-2 text-sc-ink2">{names[row.agent_id] ?? (row.agent_id || 'MCP')}</td>
								<td class="max-w-[360px] px-2 py-2">
									<span class="block truncate text-sc-ink" title={row.title}>{row.title || typeLabel(String(row.type ?? ''))}</span>
									{#if row.error && (status === 'failed' || status === 'blocked' || status === 'error')}
										<span class="block truncate font-plex-mono text-[11px] text-[#f2956f]" title={String(row.error)}>{row.error}</span>
									{:else}
										<span class="block truncate text-[11px] text-sc-ink3">{typeLabel(String(row.type ?? ''))}{row.strategy_display_id || row.strategy_id ? ` · ${row.strategy_display_id || row.strategy_id}` : ''}</span>
									{/if}
								</td>
								<td class="px-2 py-2"><span class={`inline-flex rounded border px-1.5 py-px text-[11px] ${TONE_PILL[tone(status)]}`}>{status === 'paused_manual' ? 'paused' : status}</span></td>
								<td class="whitespace-nowrap px-2 py-2 text-sc-ink3" title={shortDateTime(when(row))}>{ago(when(row), now)}</td>
								<td class="whitespace-nowrap px-2 py-2 text-right font-plex-mono text-sc-ink2">{took !== null ? fmtSeconds(took) : '—'}</td>
								<td class="whitespace-nowrap px-2 py-2 text-right font-plex-mono text-sc-ink2">{typeof row.cost_usd === 'number' && row.cost_usd > 0 ? fmtCost(row.cost_usd) : '—'}</td>
								<td class="whitespace-nowrap px-3 py-2 text-right" on:click|stopPropagation>
									{#if status === 'blocked'}
										<button type="button" class="text-[11.5px] text-[#a9cbff] hover:underline disabled:opacity-40" disabled={busy !== null} on:click={() => run('resume', [row.id])}>Resume</button>
									{/if}
									{#if DISMISSIBLE.has(status)}
										<button type="button" class="ml-2 text-[11.5px] text-sc-ink3 hover:text-sc-ink disabled:opacity-40" disabled={busy !== null} on:click={() => run('dismiss', [row.id])}>Dismiss</button>
									{/if}
								</td>
							</tr>
						{/each}
					{/if}
				</tbody>
			</table>
			{#if rows.length >= 400}
				<p class="m-0 border-t border-sc-line px-3 py-2 text-[11px] text-sc-ink3">Showing the newest 400. Narrow by status or agent to see older runs.</p>
			{/if}
		</div>

		<aside class="h-fit min-w-0 rounded-md border border-sc-line bg-sc-panel xl:sticky xl:top-2" aria-label="Run details">
			{#if !inspect}
				<p class="m-0 px-3.5 py-6 text-center text-[12px] text-sc-ink3">Select a run to see what it was doing and why it stopped.</p>
			{:else}
				{@const status = String(inspect.status)}
				<div class="grid min-w-0 gap-3 px-3.5 py-3">
					<div class="min-w-0">
						<div class="flex items-center justify-between gap-2">
							<span class="font-plex-mono text-[12px] text-sc-ink">{inspect.display_id ?? `#${inspect.id}`}</span>
							<span class={`inline-flex rounded border px-1.5 py-px text-[11px] ${TONE_PILL[tone(status)]}`}>{status}</span>
						</div>
						<p class="m-0 mt-1 text-[12.5px] text-sc-ink">{inspect.title || typeLabel(String(inspect.type ?? ''))}</p>
					</div>
					<dl class="m-0 grid grid-cols-[88px_minmax(0,1fr)] gap-x-2 gap-y-1 text-[11.5px]">
						<dt class="text-sc-ink3">Agent</dt><dd class="m-0 text-sc-ink2">{names[inspect.agent_id] ?? inspect.agent_id}</dd>
						<dt class="text-sc-ink3">Kind</dt><dd class="m-0 text-sc-ink2">{typeLabel(String(inspect.type ?? ''))}</dd>
						{#if inspect.strategy_id}<dt class="text-sc-ink3">Strategy</dt><dd class="m-0"><a class="font-plex-mono text-sc-ink hover:underline" href={`/lab/strategy/${encodeURIComponent(String(inspect.strategy_id))}`}>{inspect.strategy_display_id || inspect.strategy_id}</a>{#if inspect.strategy_stage}<span class="text-sc-ink3"> · {inspect.strategy_stage}</span>{/if}</dd>{/if}
						<dt class="text-sc-ink3">Queued</dt><dd class="m-0 text-sc-ink2">{shortDateTime(inspect.created_at)}</dd>
						<dt class="text-sc-ink3">Finished</dt><dd class="m-0 text-sc-ink2">{inspect.completed_at ? shortDateTime(inspect.completed_at) : '—'}</dd>
						<dt class="text-sc-ink3">Took</dt><dd class="m-0 text-sc-ink2">{fmtSeconds(seconds(inspect, now))}</dd>
						<dt class="text-sc-ink3">Model</dt><dd class="m-0 font-plex-mono text-sc-ink2">{[inspect.provider, inspect.model_id].filter(Boolean).join(' / ') || '—'}</dd>
						<dt class="text-sc-ink3">Usage</dt><dd class="m-0 text-sc-ink2">{fmtTokens(Number(inspect.total_tokens ?? 0))} tokens · {typeof inspect.cost_usd === 'number' ? fmtCost(inspect.cost_usd) : '—'}</dd>
						{#if Number(inspect.retry_count ?? 0) > 0}<dt class="text-sc-ink3">Retries</dt><dd class="m-0 text-sc-ink2">{inspect.retry_count}</dd>{/if}
					</dl>
					{#if inspect.error}
						<div>
							<div class="mb-1 text-[11px] text-sc-ink3">{status === 'blocked' ? 'Why it stopped' : 'Error'}</div>
							<pre class="m-0 max-h-48 overflow-auto whitespace-pre-wrap rounded border border-[#e5574f]/30 bg-[#e5574f]/[0.06] p-2 font-plex-mono text-[11px] text-[#f4b5a8] [overflow-wrap:anywhere]">{inspect.error}</pre>
						</div>
					{/if}
					{#if inspect.description}
						<details>
							<summary class="cursor-pointer text-[11px] text-sc-ink3 hover:text-sc-ink">What it was asked to do</summary>
							<pre class="m-0 mt-1 max-h-56 overflow-auto whitespace-pre-wrap rounded border border-sc-line bg-sc-bg p-2 font-plex-mono text-[11px] text-sc-ink2 [overflow-wrap:anywhere]">{inspect.description}</pre>
						</details>
					{/if}
					<div class="flex flex-wrap gap-2">
						{#if inspect.display_id}
							<a class="rounded-md bg-sc-ink px-3 py-1 text-[12px] font-medium text-black hover:bg-white" href={`/tasks/${encodeURIComponent(inspect.display_id)}?returnTo=${encodeURIComponent('/agents?tab=tasks')}`}>Open transcript</a>
						{/if}
						{#if status === 'blocked'}
							<button type="button" class="rounded-md border border-[#7fb2ff]/45 px-3 py-1 text-[12px] text-[#a9cbff] hover:border-[#7fb2ff] disabled:opacity-40" disabled={busy !== null} on:click={() => inspect && run('resume', [inspect.id])}>Resume</button>
						{/if}
						{#if DISMISSIBLE.has(status)}
							<button type="button" class="rounded-md border border-sc-line2 px-3 py-1 text-[12px] text-sc-ink2 hover:border-sc-ink3 hover:text-sc-ink disabled:opacity-40" disabled={busy !== null} on:click={() => inspect && run('dismiss', [inspect.id])}>Dismiss</button>
						{/if}
					</div>
				</div>
			{/if}
		</aside>
	</div>
</div>
