<script lang="ts">
	import { onMount } from 'svelte';
	import { getCollectionHealth, type CollectionHealth, type CollectionStream } from '$lib/api/data';

	let health: CollectionHealth | null = null;
	let loading = true;
	let error: string | null = null;

	const STATUS: Record<CollectionStream['status'], { label: string; dot: string; text: string }> = {
		healthy: { label: 'Healthy', dot: 'bg-emerald-500', text: 'text-emerald-400' },
		recovering: { label: 'Recovering', dot: 'bg-yellow-500', text: 'text-yellow-400' },
		down: { label: 'Down', dot: 'bg-red-500', text: 'text-red-400' },
		never_ran: { label: 'Idle', dot: 'bg-[#444]', text: 'text-[#555]' }
	};

	const STATUS_ORDER: Record<string, number> = { down: 0, recovering: 1, healthy: 2, never_ran: 3 };
	type SortKey = 'stream' | 'status' | 'last_success' | 'fails';
	let sortKey: SortKey = 'status';
	let sortDir: 1 | -1 = 1;

	const sortVal: Record<SortKey, (s: CollectionStream) => number | string> = {
		stream: (s) => s.stream,
		status: (s) => STATUS_ORDER[s.status] ?? 9,
		last_success: (s) => (s.last_success ? Date.parse(s.last_success) || 0 : 0),
		fails: (s) => s.consecutive_failures
	};

	function sortBy(k: SortKey) {
		if (sortKey === k) sortDir = sortDir === 1 ? -1 : 1;
		else {
			sortKey = k;
			sortDir = 1;
		}
	}

	function arrow(k: SortKey): string {
		return sortKey === k ? (sortDir === 1 ? ' ↑' : ' ↓') : '';
	}

	$: streams = health
		? [...health.streams].sort((a, b) => {
				const va = sortVal[sortKey](a);
				const vb = sortVal[sortKey](b);
				if (va < vb) return -sortDir;
				if (va > vb) return sortDir;
				return 0;
			})
		: [];

	function ago(ts: string | null): string {
		if (!ts) return '—';
		const t = Date.parse(ts);
		if (Number.isNaN(t)) return '—';
		const m = (Date.now() - t) / 60_000;
		if (m < 1) return 'now';
		return m < 60 ? `${Math.round(m)}m ago` : `${(m / 60).toFixed(1)}h ago`;
	}

	function scoreClass(score: number): string {
		if (score >= 90) return 'text-emerald-400';
		if (score >= 70) return 'text-yellow-400';
		return 'text-red-400';
	}

	async function load() {
		loading = true;
		error = null;
		try {
			health = await getCollectionHealth();
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to load source health';
		} finally {
			loading = false;
		}
	}

	onMount(load);
</script>

<div class="border border-[#222] bg-[#050505] p-4">
	<div class="mb-3 flex items-center justify-between">
		<h2 class="text-xs font-bold uppercase tracking-widest text-white">Source health</h2>
		<div class="flex items-center gap-3 text-xs">
			{#if health}
				<span class="text-[#888]">Data health</span>
				<span class="font-mono text-base font-bold {scoreClass(health.score)}">{health.score}</span>
			{/if}
			<button class="border border-[#333] px-2 py-0.5 text-[11px] text-[#888] hover:bg-[#111] hover:text-white transition-colors" on:click={load} disabled={loading}>
				{loading ? '…' : 'Refresh'}
			</button>
		</div>
	</div>

	{#if error}
		<div class="border border-red-900 bg-red-500/5 p-2 text-xs text-red-400">{error}</div>
	{:else if loading && !health}
		<div class="text-xs text-[#666]">Loading…</div>
	{:else if health}
		<div class="max-h-64 overflow-auto">
			<table class="w-full text-xs">
				<thead class="sticky top-0 bg-[#050505]">
					<tr class="text-left text-[#666]">
						<th class="py-1 pr-3 font-medium"><button class="hover:text-[#999]" on:click={() => sortBy('stream')}>stream{arrow('stream')}</button></th>
						<th class="py-1 pr-3 font-medium"><button class="hover:text-[#999]" on:click={() => sortBy('status')}>status{arrow('status')}</button></th>
						<th class="py-1 pr-3 font-medium"><button class="hover:text-[#999]" on:click={() => sortBy('last_success')}>last success{arrow('last_success')}</button></th>
						<th class="py-1 pr-3 text-right font-medium"><button class="hover:text-[#999]" on:click={() => sortBy('fails')}>fails{arrow('fails')}</button></th>
						<th class="py-1 font-medium">last error</th>
					</tr>
				</thead>
				<tbody>
					{#each streams as s}
						<tr class="border-t border-[#111]">
							<td class="py-1 pr-3 font-mono text-[#888]">{s.stream}</td>
							<td class="py-1 pr-3">
								<span class="inline-flex items-center gap-1.5 {STATUS[s.status].text}">
									<span class="inline-block h-2 w-2 rounded-full {STATUS[s.status].dot}"></span>
									{STATUS[s.status].label}
								</span>
							</td>
							<td class="py-1 pr-3 text-[#888]">{ago(s.last_success)}</td>
							<td class="py-1 pr-3 text-right font-mono {s.consecutive_failures > 0 ? 'text-red-400' : 'text-[#555]'}">
								{s.consecutive_failures}
							</td>
							<td class="max-w-[18rem] truncate py-1 text-[#666]" title={s.last_error ?? ''}>
								{s.last_error ?? '—'}
							</td>
						</tr>
					{/each}
				</tbody>
			</table>
		</div>
	{/if}
</div>
