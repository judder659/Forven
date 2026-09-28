<script lang="ts">
	// Data Log: what happened and who did it. Your actions and incidents by
	// default; automatic collection lives in its own tab, one row per run.
	import { onDestroy, onMount } from 'svelte';
	import { exportDataLog, getDataLog, type DataLogQuery } from '$lib/api/dataManager';
	import type { DataLogEntry, DataLogResponse } from '$lib/api/dataManagerTypes';
	import SectionState from '$lib/components/data-manager/SectionState.svelte';
	import { runAction, saveBlob } from '$lib/components/data-manager/actions';
	import { formatCount, formatRelative, formatUtc, originLabel } from '$lib/components/data-manager/format';
	import { catalogHref, seriesHref } from '$lib/components/data-manager/links';
	import { clock, createRequestGuard, loading, pageSearch, settle, type Loadable } from '$lib/stores/dataManager';

	const LIMIT = 50;
	const LEVELS: DataLogEntry['level'][] = ['info', 'warning', 'error'];
	const LEVEL_CLASS: Record<DataLogEntry['level'], string> = {
		info: 'border-[#333] text-[#999]',
		warning: 'border-amber-900 text-amber-400',
		error: 'border-red-900 text-red-400',
	};

	let tab: 'mine' | 'auto' = 'mine';
	let levels: DataLogEntry['level'][] = [];
	let symbol = '';
	let action = '';
	let since = '';
	let until = '';
	let q = '';
	let offset = 0;
	let log: Loadable<DataLogResponse> = loading();
	let exporting = false;
	let searchInput: HTMLInputElement | undefined;
	const guard = createRequestGuard();
	let typingTimer: ReturnType<typeof setTimeout> | undefined;

	function query(): DataLogQuery {
		return {
			category: tab === 'mine' ? ['user', 'incident'] : ['routine'],
			level: levels.length ? levels : undefined,
			symbol: symbol.trim() ? symbol.trim().toUpperCase().replace(/[/_ ]+/g, '-') : undefined,
			action: action || undefined,
			since: since ? `${since}T00:00:00Z` : undefined,
			until: until ? `${until}T23:59:59Z` : undefined,
			q: q.trim() || undefined,
			limit: LIMIT,
			offset,
		};
	}

	async function load() {
		const { signal, current } = guard.next();
		const next = await settle(getDataLog(query(), signal), log);
		if (current()) log = next;
	}

	$: filterKey = JSON.stringify([tab, levels, action, since, until]);
	let appliedKey = '';
	$: if (filterKey !== appliedKey) {
		appliedKey = filterKey;
		offset = 0;
		void load();
	}
	function typed() {
		clearTimeout(typingTimer);
		typingTimer = setTimeout(() => {
			offset = 0;
			void load();
		}, 300);
	}
	function page(delta: number) {
		offset = Math.max(0, offset + delta * LIMIT);
		void load();
	}
	function toggleLevel(level: DataLogEntry['level']) {
		levels = levels.includes(level) ? levels.filter((l) => l !== level) : [...levels, level];
	}
	async function exportCsv() {
		exporting = true;
		await runAction('Exporting the log', async () => {
			const blob = await exportDataLog(query());
			saveBlob(blob, `forven-data-log-${tab === 'mine' ? 'actions' : 'automatic'}-${new Date().toISOString().slice(0, 10)}.csv`);
		}, { success: () => 'Exported the log as CSV.', poke: false });
		exporting = false;
	}

	onMount(() => pageSearch.set(searchInput ?? null));
	onDestroy(() => {
		pageSearch.set(null);
		guard.cancel();
		clearTimeout(typingTimer);
	});

	$: entries = log.data?.entries ?? [];
	$: actions = [...new Set(entries.map((e) => e.action))].sort();
	const tabClass = (on: boolean) =>
		`-mb-px border-b-2 px-3 py-1.5 text-[10px] font-bold uppercase tracking-wider ${on ? 'border-white text-white' : 'border-transparent text-[#555] hover:text-[#aaa]'}`;
</script>

<svelte:head><title>Data · Log | Forven</title></svelte:head>

<div class="space-y-3 p-4 pb-24">
	<div class="flex items-end border-b border-[#1a1a1a]" role="tablist" aria-label="Log">
		<button type="button" role="tab" aria-selected={tab === 'mine'} class={tabClass(tab === 'mine')} on:click={() => (tab = 'mine')}>Your actions & incidents</button>
		<button type="button" role="tab" aria-selected={tab === 'auto'} class={tabClass(tab === 'auto')} on:click={() => (tab = 'auto')}>Automatic</button>
	</div>

	<div class="flex flex-wrap items-center gap-2">
		<input bind:this={searchInput} bind:value={q} on:input={typed} type="search" placeholder="Search messages" aria-label="Search the log" spellcheck="false"
			class="w-56 border border-[#2a2a2a] bg-black px-2 py-1 text-[11px] text-white outline-none placeholder:text-[#555] focus:border-white" />
		<div class="flex gap-1" role="group" aria-label="Level">
			{#each LEVELS as level}
				<button type="button" aria-pressed={levels.includes(level)} on:click={() => toggleLevel(level)}
					class="border px-2 py-1 text-[10px] uppercase tracking-wider {levels.includes(level) ? 'border-white bg-white text-black' : `${LEVEL_CLASS[level]} hover:border-[#666]`}">{level === 'warning' ? 'Warn' : level}</button>
			{/each}
		</div>
		<input bind:value={symbol} on:input={typed} placeholder="Symbol" aria-label="Filter by symbol" spellcheck="false"
			class="w-28 border border-[#2a2a2a] bg-black px-2 py-1 font-mono text-[11px] text-white outline-none placeholder:text-[#555] focus:border-white" />
		<select bind:value={action} aria-label="Filter by action" class="border border-[#2a2a2a] bg-black px-1.5 py-1 text-[10px] text-[#ccc] outline-none focus:border-white">
			<option value="">All actions</option>
			{#each actions as a}<option value={a}>{a.replaceAll('_', ' ')}</option>{/each}
			{#if action && !actions.includes(action)}<option value={action}>{action.replaceAll('_', ' ')}</option>{/if}
		</select>
		<label class="flex items-center gap-1 text-[10px] uppercase tracking-wider text-[#666]">From
			<input type="date" bind:value={since} max={until || undefined} class="border border-[#2a2a2a] bg-black px-1.5 py-0.5 font-mono text-[11px] text-[#ccc] outline-none [color-scheme:dark] focus:border-white" /></label>
		<label class="flex items-center gap-1 text-[10px] uppercase tracking-wider text-[#666]">To
			<input type="date" bind:value={until} min={since || undefined} class="border border-[#2a2a2a] bg-black px-1.5 py-0.5 font-mono text-[11px] text-[#ccc] outline-none [color-scheme:dark] focus:border-white" /></label>
		<span class="text-[10px] text-[#555]">UTC</span>
		<button type="button" on:click={exportCsv} disabled={exporting || log.status !== 'ready'} class="terminal-button ml-auto text-[10px]">{exporting ? 'Exporting…' : 'Export CSV'}</button>
	</div>

	<section class="border border-[#222] bg-[#050505]" aria-label="Log entries">
		<SectionState state={log} what="The data log" endpoint="GET /api/data/log" rows={8} on:retry={load}>
			{#if !entries.length}
				<p class="px-4 py-8 text-center text-[12px] text-[#777]">
					{tab === 'mine' ? 'Nothing you started and no incidents match these filters.' : 'No automatic runs match these filters.'}
				</p>
			{:else}
				<table class="w-full text-[11px]">
					<thead>
						<tr class="border-b border-[#141414] bg-[#0a0a0a] text-[9px] uppercase tracking-wider text-[#555]">
							<th class="px-3 py-1.5 text-left font-normal">When (UTC)</th>
							<th class="px-2 py-1.5 text-left font-normal">Level</th>
							<th class="px-2 py-1.5 text-left font-normal">What happened</th>
							<th class="px-2 py-1.5 text-left font-normal">Series</th>
							<th class="px-3 py-1.5 text-left font-normal">By</th>
						</tr>
					</thead>
					<tbody>
						{#each entries as entry (entry.id)}
							<tr class="border-b border-[#101010] align-top hover:bg-white/[0.02]">
								<td class="whitespace-nowrap px-3 py-1.5">
									<div class="font-mono text-[10px] text-[#bbb]">{formatUtc(entry.ts, { suffix: false })}</div>
									<div class="text-[10px] text-[#555]">{formatRelative(entry.ts, $clock)}</div>
								</td>
								<td class="px-2 py-1.5"><span class="border px-1 py-px text-[9px] font-bold uppercase tracking-wider {LEVEL_CLASS[entry.level]}">{entry.level === 'warning' ? 'warn' : entry.level}</span></td>
								<td class="px-2 py-1.5">
									<div class="text-[#ddd]">{entry.message}</div>
									<div class="text-[10px] text-[#555]">{entry.category} · {entry.action.replaceAll('_', ' ')}{entry.children ? ` · ${formatCount(entry.children)} series in this run` : ''}{entry.job_id ? ` · job ${entry.job_id}` : ''}</div>
								</td>
								<td class="whitespace-nowrap px-2 py-1.5">
									{#if entry.symbol && entry.timeframe}
										<a href={seriesHref({ symbol: entry.symbol, timeframe: entry.timeframe })} class="font-mono text-[10px] text-[#aaa] hover:text-white hover:underline">{entry.symbol} {entry.timeframe}</a>
									{:else if entry.symbol}
										<a href={catalogHref({ q: entry.symbol })} class="font-mono text-[10px] text-[#aaa] hover:text-white hover:underline">{entry.symbol}</a>
									{:else}<span class="text-[#444]">—</span>{/if}
								</td>
								<td class="whitespace-nowrap px-3 py-1.5 text-[10px] text-[#888]">{originLabel(entry.origin)}</td>
							</tr>
						{/each}
					</tbody>
				</table>
				<div class="flex items-center gap-2 px-3 py-1.5 text-[10px] text-[#666]">
					<span>{formatCount(offset + 1)}–{formatCount(offset + entries.length)} of {formatCount(log.data?.total ?? 0)}</span>
					<button type="button" on:click={() => page(-1)} disabled={offset === 0} class="ml-auto border border-[#2a2a2a] px-2 py-0.5 text-[#aaa] hover:border-white hover:text-white disabled:opacity-30" aria-label="Newer entries">← Newer</button>
					<button type="button" on:click={() => page(1)} disabled={offset + LIMIT >= (log.data?.total ?? 0)} class="border border-[#2a2a2a] px-2 py-0.5 text-[#aaa] hover:border-white hover:text-white disabled:opacity-30" aria-label="Older entries">Older →</button>
				</div>
			{/if}
		</SectionState>
	</section>
</div>
