<script lang="ts">
	import { onMount } from 'svelte';
	import { getDataActivity, type DataActivity, type DataActivityEvent } from '$lib/api/data';

	let activity: DataActivity | null = null;
	let loading = true;
	let error: string | null = null;
	let actionFilter = 'all';
	let search = '';

	const ACTION_META: Record<string, { label: string; icon: string; tone: string }> = {
		download: { label: 'Download', icon: '↓', tone: 'border-[#333] text-[#888]' },
		backfill: { label: 'Backfill', icon: '⛏', tone: 'border-[#333] text-[#888]' },
		source_reconciliation: { label: 'Reconcile', icon: '⇄', tone: 'border-[#333] text-[#888]' },
		csv_upload: { label: 'CSV upload', icon: '⇪', tone: 'border-emerald-900 bg-emerald-500/10 text-emerald-400' },
		dataset_delete: { label: 'Delete', icon: '✕', tone: 'border-red-900 bg-red-500/10 text-red-400' },
		orphan_scan: { label: 'Orphan scan', icon: '⚠', tone: 'border-yellow-900 bg-yellow-500/10 text-yellow-400' },
		orphan_cleanup: { label: 'Cleanup', icon: '♻', tone: 'border-yellow-900 bg-yellow-500/10 text-yellow-400' },
		event: { label: 'Event', icon: '•', tone: 'border-[#333] text-[#888]' }
	};

	function meta(action: string) {
		return ACTION_META[action] ?? ACTION_META.event;
	}

	function levelClass(level: string): string {
		if (level === 'error') return 'text-red-400';
		if (level === 'warning') return 'text-yellow-400';
		return 'text-[#888]';
	}

	function ago(ts: string | null): string {
		if (!ts) return '—';
		const t = Date.parse(ts.includes('T') ? ts : `${ts.replace(' ', 'T')}Z`);
		if (Number.isNaN(t)) return '—';
		const m = (Date.now() - t) / 60_000;
		if (m < 1) return 'just now';
		if (m < 60) return `${Math.round(m)}m ago`;
		const h = m / 60;
		if (h < 24) return `${h.toFixed(1)}h ago`;
		return `${(h / 24).toFixed(1)}d ago`;
	}

	$: events = activity?.events ?? [];
	$: actions = Array.from(new Set(events.map((e) => e.action)));
	$: query = search.trim().toLowerCase();
	$: shown = events.filter((e) => {
		if (actionFilter !== 'all' && e.action !== actionFilter) return false;
		if (!query) return true;
		const sym = String(e.detail?.symbol ?? '');
		return `${e.message} ${e.action} ${sym}`.toLowerCase().includes(query);
	});

	async function load() {
		loading = true;
		error = null;
		try {
			activity = await getDataActivity(200);
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to load activity';
		} finally {
			loading = false;
		}
	}

	onMount(load);
</script>

<div class="terminal-card">
	<div class="flex flex-wrap items-center justify-between gap-3 border-b border-[#1a1a1a] px-4 py-2">
		<h2 class="text-[10px] font-bold uppercase tracking-widest text-[#888]">Activity log</h2>
		<div class="flex flex-wrap items-center gap-2 text-xs">
			<input
				type="text"
				bind:value={search}
				placeholder="Search symbol, action…"
				class="terminal-input w-40 px-2 py-0.5 text-[11px]"
			/>
			<select
				bind:value={actionFilter}
				class="terminal-select w-auto px-2 py-0.5 text-[11px]"
			>
				<option value="all">All actions</option>
				{#each actions as a}
					<option value={a}>{meta(a).label}</option>
				{/each}
			</select>
			<button class="terminal-button px-2 py-0.5 text-[11px]" on:click={load} disabled={loading}>
				{loading ? '…' : 'Refresh'}
			</button>
		</div>
	</div>
	<p class="border-b border-[#1a1a1a] px-4 py-2 text-[11px] text-[#666]">Every download, backfill, upload, delete, reconciliation, and cleanup, newest first.</p>

	{#if error}
		<div class="m-3 border border-red-900 bg-red-500/5 px-4 py-2 text-xs text-red-400">{error}</div>
	{:else if loading && events.length === 0}
		<div class="px-4 py-3 text-xs uppercase tracking-widest text-[#555]">Loading activity…</div>
	{:else if shown.length === 0}
		<div class="px-4 py-3 text-xs text-[#666]">
			{events.length === 0
				? 'No data actions logged yet. Download a dataset or backfill a series and it will appear here.'
				: 'No activity for this filter.'}
		</div>
	{:else}
		<ol class="relative space-y-0">
			{#each shown as event}
				{@const m = meta(event.action)}
				<li class="flex items-start gap-3 border-t border-[#111] px-4 py-2 transition-colors first:border-t-0 hover:bg-[#111]">
					<span class="mt-0.5 inline-flex h-5 w-5 shrink-0 items-center justify-center border text-[11px] {m.tone}" title={m.label}>
						{m.icon}
					</span>
					<div class="min-w-0 flex-1">
						<div class="text-xs {levelClass(event.level)}">{event.message}</div>
						{#if event.detail?.error}
							<div class="mt-0.5 truncate font-mono text-[11px] text-red-400/80" title={String(event.detail.error)}>
								{event.detail.error}
							</div>
						{/if}
					</div>
					<span class="shrink-0 whitespace-nowrap text-[11px] text-[#666]" title={event.ts ?? ''}>{ago(event.ts)}</span>
				</li>
			{/each}
		</ol>
	{/if}
</div>
