<script lang="ts">
	import { onMount } from 'svelte';
	import {
		getDataHealth,
		scanOrphans,
		cleanupOrphans,
		getDatasetVersions,
		type DataHealth,
		type OrphanReport,
		type DatasetVersion
	} from '$lib/api/data';

	let health: DataHealth | null = null;
	let orphans: OrphanReport | null = null;
	let versions: DatasetVersion[] = [];
	let loading = true;
	let error: string | null = null;
	let scanning = false;
	let cleaning = false;
	let notice: string | null = null;

	function fmtBytes(n: number | null | undefined): string {
		if (!n) return '0 B';
		const u = ['B', 'KB', 'MB', 'GB', 'TB'];
		let i = 0;
		let v = n;
		while (v >= 1024 && i < u.length - 1) {
			v /= 1024;
			i++;
		}
		return `${v.toFixed(v < 10 && i > 0 ? 1 : 0)} ${u[i]}`;
	}

	function ago(ts: string | null): string {
		if (!ts) return '—';
		const t = Date.parse(ts);
		if (Number.isNaN(t)) return '—';
		const m = (Date.now() - t) / 60_000;
		if (m < 1) return 'now';
		if (m < 60) return `${Math.round(m)}m ago`;
		const h = m / 60;
		return h < 24 ? `${h.toFixed(1)}h ago` : `${(h / 24).toFixed(1)}d ago`;
	}

	async function load() {
		loading = true;
		error = null;
		try {
			[health, versions] = await Promise.all([getDataHealth(), getDatasetVersions({ limit: 12 })]);
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to load storage health';
		} finally {
			loading = false;
		}
	}

	async function doScan() {
		scanning = true;
		notice = null;
		error = null;
		try {
			orphans = await scanOrphans();
			const total = orphans.orphans.length + orphans.cataloged_missing.length;
			notice = total === 0 ? 'No storage drift — catalog and parquet are in sync. ✓' : null;
		} catch (e) {
			error = e instanceof Error ? e.message : 'Orphan scan failed';
		} finally {
			scanning = false;
		}
	}

	async function doCleanup() {
		if (cleaning) return;
		cleaning = true;
		notice = null;
		error = null;
		try {
			const res = await cleanupOrphans();
			const reviewNote = res.skipped > 0 ? ` ${res.skipped} left for manual review.` : '';
			notice =
				res.removed === 0
					? `Nothing auto-removed.${reviewNote}`
					: `Removed ${res.removed} orphaned file${res.removed === 1 ? '' : 's'} (${fmtBytes(res.bytes_freed)} freed).${reviewNote}`;
			orphans = await scanOrphans();
			await load();
		} catch (e) {
			error = e instanceof Error ? e.message : 'Cleanup failed';
		} finally {
			cleaning = false;
		}
	}

	onMount(load);
</script>

<div class="border border-[#222] bg-[#050505] p-4">
	<div class="mb-3 flex items-center justify-between">
		<h2 class="text-xs font-bold uppercase tracking-widest text-white">Storage &amp; maintenance</h2>
		<button class="border border-[#333] px-2 py-0.5 text-[11px] text-[#888] hover:bg-[#111] hover:text-white transition-colors" on:click={load} disabled={loading}>
			{loading ? '…' : 'Refresh'}
		</button>
	</div>

	{#if error}
		<div class="mb-2 border border-red-900 bg-red-500/5 p-2 text-xs text-red-400">{error}</div>
	{/if}
	{#if notice}
		<div class="mb-2 border border-emerald-900 bg-emerald-500/5 p-2 text-xs text-emerald-400">{notice}</div>
	{/if}

	{#if loading && !health}
		<div class="text-xs text-[#666]">Loading…</div>
	{:else if health}
		<!-- Storage summary -->
		<div class="mb-4 grid grid-cols-2 gap-x-4 gap-y-2 text-xs sm:grid-cols-3">
			<div>
				<div class="text-[#666]">Datasets</div>
				<div class="font-mono text-[#888]">{health.dataset_count}</div>
			</div>
			<div>
				<div class="text-[#666]">Parquet files</div>
				<div class="font-mono text-[#888]">{health.total_parquet_files}</div>
			</div>
			<div>
				<div class="text-[#666]">Parquet size</div>
				<div class="font-mono text-[#888]">{fmtBytes(health.total_parquet_bytes)}</div>
			</div>
			<div>
				<div class="text-[#666]">DB size</div>
				<div class="font-mono text-[#888]">{fmtBytes(health.db_size_bytes)}{health.wal_present ? ` (+${fmtBytes(health.wal_size_bytes)} WAL)` : ''}</div>
			</div>
			<div>
				<div class="text-[#666]">Avg quality</div>
				<div class="font-mono {health.quality_avg_score == null ? 'text-[#666]' : health.quality_avg_score >= 90 ? 'text-emerald-400' : health.quality_avg_score >= 70 ? 'text-yellow-400' : 'text-red-300'}">
					{health.quality_avg_score == null ? '—' : health.quality_avg_score.toFixed(0)}
				</div>
			</div>
			<div>
				<div class="text-[#666]">Last ingestion</div>
				<div class="font-mono text-[#888]">{ago(health.last_ingestion_at)}</div>
			</div>
		</div>

		<!-- Orphan scan -->
		<div class="mb-4 border-t border-[#1a1a1a] pt-3">
			<div class="mb-2 flex items-center justify-between">
				<span class="text-xs font-medium text-[#888]">
					Storage drift
					{#if health.orphan_count > 0}
						<span class="ml-1 border border-yellow-900 px-1.5 py-0.5 text-yellow-400">{health.orphan_count} flagged</span>
					{/if}
				</span>
				<div class="flex items-center gap-2">
					{#if orphans && orphans.orphans.some((o) => o.safe_delete)}
						<button class="border border-yellow-800 px-2 py-0.5 text-[11px] text-yellow-400 hover:bg-[#111] transition-colors disabled:opacity-50" on:click={doCleanup} disabled={cleaning || scanning}>
							{cleaning ? 'cleaning…' : `Clean up ${orphans.orphans.filter((o) => o.safe_delete).length}`}
						</button>
					{/if}
					<button class="border border-[#333] px-2 py-0.5 text-[11px] text-[#888] hover:bg-[#111] hover:text-white transition-colors disabled:opacity-50" on:click={doScan} disabled={scanning || cleaning}>
						{scanning ? 'scanning…' : 'Scan orphans'}
					</button>
				</div>
			</div>
			{#if orphans}
				{#if orphans.orphans.length === 0 && orphans.cataloged_missing.length === 0}
					<div class="text-xs text-[#666]">In sync — no orphaned files or missing parquet.</div>
				{:else}
					<div class="space-y-1 text-xs">
						{#each orphans.orphans as o}
							<div class="flex items-center justify-between {o.safe_delete ? 'text-yellow-400' : 'text-[#888]'}">
								<span class="font-mono">{o.symbol}/{o.timeframe}</span>
								<span class="text-[#666]">{o.reason ?? 'orphan'} · {fmtBytes(o.size_bytes)}{o.safe_delete ? '' : ' · kept'}</span>
							</div>
						{/each}
						{#each orphans.cataloged_missing as m}
							<div class="flex items-center justify-between text-red-400">
								<span class="font-mono">{m.symbol}/{m.timeframe}</span>
								<span class="text-[#666]">cataloged but file missing</span>
							</div>
						{/each}
					</div>
				{/if}
			{/if}
		</div>

		<!-- Recent versions audit trail -->
		<div class="border-t border-[#1a1a1a] pt-3">
			<div class="mb-2 text-xs font-medium text-[#888]">Recent ingestion versions</div>
			{#if versions.length === 0}
				<div class="text-xs text-[#666]">No version history yet.</div>
			{:else}
				<div class="overflow-x-auto">
					<table class="w-full text-xs">
						<thead>
							<tr class="text-left text-[#666]">
								<th class="py-1 pr-3 font-medium">series</th>
								<th class="py-1 pr-3 font-medium">source</th>
								<th class="py-1 pr-3 font-medium text-right">rows</th>
								<th class="py-1 font-medium text-right">created</th>
							</tr>
						</thead>
						<tbody>
							{#each versions as v}
								<tr class="border-t border-[#111]">
									<td class="py-1 pr-3 font-mono text-[#888]">{v.symbol}<span class="text-[#555]">/{v.timeframe}</span></td>
									<td class="py-1 pr-3 text-[#888]">{v.source}</td>
									<td class="py-1 pr-3 text-right font-mono text-[#888]">{v.row_count.toLocaleString()}</td>
									<td class="py-1 text-right text-[#666]">{ago(v.created_at)}</td>
								</tr>
							{/each}
						</tbody>
					</table>
				</div>
			{/if}
		</div>
	{/if}
</div>
