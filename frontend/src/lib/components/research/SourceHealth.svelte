<script lang="ts">
	import { onMount, createEventDispatcher } from 'svelte';
	import { createPoller } from '$lib/utils/polling';
	import { getCollectionHealth, type CollectionHealth, type CollectionStream } from '$lib/api/data';

	const dispatch = createEventDispatcher<{ activity: void; maintenance: void }>();
	let checkedAt: string | null = null;
	let disposed = false;
	let inFlight = false;
	let health: CollectionHealth | null = null;
	let loading = true;
	let error: string | null = null;

	const STATUS: Record<CollectionStream['status'], { label: string; dot: string; text: string }> = {
		healthy: { label: 'No consecutive failures', dot: 'bg-emerald-500', text: 'text-emerald-400' },
		recovering: { label: 'Recovering', dot: 'bg-yellow-500', text: 'text-yellow-400' },
		down: { label: 'Down', dot: 'bg-red-500', text: 'text-red-400' },
		never_ran: { label: 'Not observed', dot: 'bg-[#444]', text: 'text-[#555]' }
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
		if (inFlight || disposed) return;
		inFlight = true; loading = true;
		try {
			const next = await getCollectionHealth();
			if (disposed) return;
			health = next; checkedAt = new Date().toISOString(); error = null;
		} catch (e) {
			if (!disposed) error = e instanceof Error ? e.message : 'Failed to load collection status';
		} finally {
			inFlight = false;
			if (!disposed) loading = false;
		}
	}
	$: problems = health?.streams.filter(stream => stream.status === 'down' || stream.status === 'recovering').length ?? 0;
	$: unobserved = health?.streams.filter(stream => stream.status === 'never_ran').length ?? 0;
	onMount(() => {
		const poller = createPoller(load, 30_000);
		poller.start();
		return () => { disposed = true; poller.stop(); };
	});

</script>

<section class="border border-[#222] bg-[#0a0a0a] p-4" aria-label="Automatic data collection">
	<div class="mb-3 flex items-center justify-between">
		<h2 class="text-xs font-bold uppercase tracking-widest text-white">Collection status</h2>
		<div class="flex items-center gap-3 text-xs">
			{#if health && !error}
				<span class="text-[#888]" title="Based on consecutive collection failures; not dataset coverage or strategy readiness">Collection reliability</span>
				{#if health.score != null}
					<span class="font-mono text-base font-bold {scoreClass(health.score)}">{health.score}/100</span>
				{:else}
					<span class="text-[#888]">Not established</span>
				{/if}
			{/if}
			<button class="border border-[#333] px-2 py-0.5 text-[11px] text-[#888] hover:bg-[#111] hover:text-white transition-colors" on:click={load} disabled={loading}>
				{loading ? '…' : 'Refresh'}
			</button>
		</div>
	</div>

	<p class="mb-3 text-sm text-gray-400">Recent collector results, refreshed every 30 seconds while this tab is visible. Collection success does not establish complete or current data for every market.</p>
	{#if checkedAt}<p class="mb-3 text-xs text-gray-500">{error ? 'Last successful check' : 'Status checked'}: {new Date(checkedAt).toLocaleString()}</p>{/if}
	{#if health && !error}<p class="mb-3 text-sm text-gray-300">{problems} streams reporting repeated failures · {unobserved} not observed. Check the coverage matrix below for market and timeframe freshness.</p>{/if}
	<div class="mb-4 flex flex-wrap gap-4 text-sm"><button class="text-cyan-300 underline" on:click={() => dispatch('activity')}>Inspect collection activity</button><button class="text-cyan-300 underline" on:click={() => dispatch('maintenance')}>Manage coverage & history</button></div>
	{#if error}
		<div class="border border-red-900 bg-red-500/5 p-2 text-xs text-red-400">Collection status unavailable: {error}. Retry with Refresh; previous results are not a current health check.</div>
	{:else if loading && !health}
		<div class="text-xs text-[#666]">Loading…</div>
	{:else if health}
		<details open={problems > 0}><summary class="mb-3 cursor-pointer text-sm text-gray-300">Inspect {streams.length} collection streams</summary>
		<p class="mb-2 text-xs text-gray-500">A recorded error may predate a later success. Unobserved optional streams do not necessarily block a strategy.</p>
		<div class="max-h-64 overflow-auto">
			<table class="w-full text-xs">
				<thead class="sticky top-0 bg-[#050505]">
					<tr class="text-left text-[#666]">
						<th class="py-1 pr-3 font-medium"><button class="hover:text-[#999]" on:click={() => sortBy('stream')}>stream{arrow('stream')}</button></th>
						<th class="py-1 pr-3 font-medium"><button class="hover:text-[#999]" on:click={() => sortBy('status')}>status{arrow('status')}</button></th>
						<th class="py-1 pr-3 font-medium"><button class="hover:text-[#999]" on:click={() => sortBy('last_success')}>last success{arrow('last_success')}</button></th>
						<th class="py-1 pr-3 text-right font-medium"><button class="hover:text-[#999]" on:click={() => sortBy('fails')}>fails{arrow('fails')}</button></th>
						<th class="py-1 font-medium">last recorded error</th>
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
		</details>
	{/if}
</section>
