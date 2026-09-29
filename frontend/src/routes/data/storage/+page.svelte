<script lang="ts">
	// Storage: where the disk goes and what can be reclaimed safely. Reclaim
	// moves items to the trash (restorable until the retention runs out);
	// emptying the trash is the only permanent step and needs a typed confirmation.
	// The identity audit is a report: nothing here moves files on its own.
	import { onMount } from 'svelte';
	import { getIdentityAudit, getStorage, getTrash, purgeTrash, reclaimStorage, restoreTrashItem } from '$lib/api/dataManager';
	import type { IdentityAuditResponse, ReclaimGroup, StorageInventory, TrashResponse } from '$lib/api/dataManagerTypes';
	import SectionState from '$lib/components/data-manager/SectionState.svelte';
	import TypedConfirm from '$lib/components/data-manager/TypedConfirm.svelte';
	import { runAction } from '$lib/components/data-manager/actions';
	import { formatBytes, formatCount, formatRelative, formatUtc, plural, streamLabel } from '$lib/components/data-manager/format';
	import { seriesHref } from '$lib/components/data-manager/links';
	import { clock, jobsLanded, loading, settle, type Loadable } from '$lib/stores/dataManager';

	let storage: Loadable<StorageInventory> = loading();
	let trash: Loadable<TrashResponse> = loading();
	let audit: Loadable<IdentityAuditResponse> = loading();
	let expanded: Record<string, boolean> = {};
	let confirming: ReclaimGroup['kind'] | null = null;
	let reclaiming = false;
	let confirmPurge = false;
	let purging = false;
	let restoring: Record<string, boolean> = {};

	const loadStorage = async () => (storage = await settle(getStorage(), storage));
	const loadTrash = async () => (trash = await settle(getTrash(), trash));
	const loadAudit = async () => (audit = await settle(getIdentityAudit(), audit));
	onMount(() => void Promise.all([loadStorage(), loadTrash(), loadAudit()]));

	// Work landed: a reclaim, delete or download changes the totals and the trash.
	let landedSeen = $jobsLanded;
	$: if ($jobsLanded !== landedSeen) {
		landedSeen = $jobsLanded;
		void Promise.all([loadStorage(), loadTrash()]);
	}

	$: s = storage.data;
	$: diskUsed = s ? s.disk.total_bytes - s.disk.free_bytes : 0;
	$: lowDisk = s ? s.disk.free_bytes < s.disk.min_free_gb * 1024 ** 3 : false;
	$: streamMax = Math.max(1, ...(s?.by_stream.map((b) => b.bytes) ?? [1]));
	$: reclaimTotal = s?.reclaimable.reduce((sum, g) => sum + g.bytes, 0) ?? 0;

	async function reclaim(group: ReclaimGroup) {
		reclaiming = true;
		const job = await runAction(`Reclaiming ${group.label.toLowerCase()}`, () => reclaimStorage({ kind: group.kind, item_ids: 'all', confirm: `reclaim ${group.kind}` }), {
			success: () =>
				group.kind === 'revisions'
					? `Pruning old revision entries (${formatBytes(group.bytes)}). Progress is in Jobs.`
					: `Moving ${plural(group.count, 'item')} (${formatBytes(group.bytes)}) to the trash. Progress is in Jobs.`,
		});
		reclaiming = false;
		if (job) {
			confirming = null;
			void loadStorage();
			void loadTrash();
		}
	}

	async function restore(id: string, label: string) {
		restoring = { ...restoring, [id]: true };
		const done = await runAction('Restoring', () => restoreTrashItem(id), { success: () => `Restored ${label}.`, poke: false });
		restoring = { ...restoring, [id]: false };
		if (done !== null) {
			void loadTrash();
			void loadStorage();
		}
	}

	async function purge() {
		purging = true;
		const done = await runAction('Emptying the trash', () => purgeTrash({ item_ids: 'all', confirm: 'empty trash' }), {
			success: () => 'Emptied the trash. Those files are gone for good.',
			poke: false,
		});
		purging = false;
		if (done !== null) {
			confirmPurge = false;
			void loadTrash();
			void loadStorage();
		}
	}

	// A countdown rounds up: an item trashed just now under a 7-day retention
	// goes in 7 d, not 6.
	function purgeIn(iso: string, now: number): string {
		const left = Date.parse(iso) - now;
		if (!Number.isFinite(left) || left <= 0) return 'at the next cleanup';
		const days = left / 86_400_000;
		return days >= 1 ? `in ${Math.ceil(days)} d` : `in ${Math.max(1, Math.ceil(left / 3_600_000))} h`;
	}

	const AUDIT_LABEL: Record<string, string> = {
		alias_duplicate: 'Same instrument, two names',
		unknown_symbol: 'Unknown symbol',
		empty_dir: 'Empty folder',
		stray_dir: 'Not a symbol',
		delisted_collected: 'Delisted but collected',
		unstamped: 'No provenance stamp',
	};
</script>

<svelte:head><title>Data · Storage | Forven</title></svelte:head>

<div class="space-y-3 p-4 pb-24">
	<section class="rounded-md border border-sc-line bg-sc-panel" aria-labelledby="dm-storage-top">
		<h2 id="dm-storage-top" class="sr-only">Disk and lake</h2>
		<SectionState state={storage} what="The storage inventory" endpoint="GET /api/data/storage" rows={3} on:retry={loadStorage}>
			{#if s}
				<div class="grid grid-cols-2 gap-px bg-sc-raise md:grid-cols-4">
					<div class="bg-sc-panel px-3 py-2.5">
						<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Lake</div>
						<div class="font-mono text-[18px] tabular-nums text-sc-ink">{formatBytes(s.lake.bytes)}</div>
						<div class="text-[10px] text-sc-ink3">{formatCount(s.lake.series)} series · {formatCount(s.lake.files)} files</div>
						<div class="mt-0.5 truncate font-mono text-[9px] text-sc-ink4" title={s.data_root}>{s.data_root}</div>
					</div>
					<div class="bg-sc-panel px-3 py-2.5">
						<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Disk free</div>
						<div class="font-mono text-[18px] tabular-nums {lowDisk ? 'text-red-400' : 'text-sc-ink'}">{formatBytes(s.disk.free_bytes)}</div>
						<div class="mt-1 h-1 bg-sc-raise" title="{formatBytes(diskUsed)} used of {formatBytes(s.disk.total_bytes)}"><div class="h-full {lowDisk ? 'bg-red-500' : 'bg-sc-ink3'}" style="width: {(diskUsed / Math.max(1, s.disk.total_bytes)) * 100}%"></div></div>
						<div class="mt-0.5 text-[10px] text-sc-ink3">of {formatBytes(s.disk.total_bytes)} · jobs stop below {s.disk.min_free_gb} GB free</div>
					</div>
					<div class="bg-sc-panel px-3 py-2.5">
						<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Reclaimable</div>
						<div class="font-mono text-[18px] tabular-nums text-sc-ink">{formatBytes(reclaimTotal)}</div>
						<div class="text-[10px] text-sc-ink3">in {s.reclaimable.filter((g) => g.count).length} groups below</div>
					</div>
					<div class="bg-sc-panel px-3 py-2.5">
						<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Trash</div>
						<div class="font-mono text-[18px] tabular-nums text-sc-ink">{formatBytes(s.trash.bytes)}</div>
						<div class="text-[10px] text-sc-ink3">{plural(s.trash.items, 'item')} · kept {s.trash.retention_days} days</div>
					</div>
				</div>
			{/if}
		</SectionState>
	</section>

	{#if s}
		<div class="grid gap-3 xl:grid-cols-2">
			<section class="rounded-md border border-sc-line bg-sc-panel" aria-labelledby="dm-by-stream">
				<header class="border-b border-sc-line px-3 py-1.5"><h2 id="dm-by-stream" class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink">Size by stream</h2></header>
				<div class="space-y-1 px-3 py-2">
					{#each s.by_stream as b (b.stream)}
						<div class="grid grid-cols-[110px_1fr_76px_70px] items-center gap-2 text-[11px]">
							<span class="text-sc-ink">{streamLabel(b.stream)}</span>
							<div class="h-1.5 bg-sc-raise"><div class="h-full bg-sc-ink3" style="width: {(b.bytes / streamMax) * 100}%"></div></div>
							<span class="text-right font-mono text-[10px] tabular-nums text-sc-ink">{formatBytes(b.bytes)}</span>
							<span class="text-right text-[10px] text-sc-ink3">{plural(b.files, 'file')}</span>
						</div>
					{/each}
					<div class="pt-1 text-[10px] text-sc-ink3">Revision log: {formatBytes(s.revisions.bytes)} in {formatCount(s.revisions.files)} files since {formatUtc(s.revisions.oldest, { date: true })}; entries older than {s.revisions.keep_days} days can be pruned{s.revisions.prunable_bytes != null ? ` (${formatBytes(s.revisions.prunable_bytes)})` : ''}.</div>
				</div>
			</section>
			<section class="rounded-md border border-sc-line bg-sc-panel" aria-labelledby="dm-top-series">
				<header class="border-b border-sc-line px-3 py-1.5"><h2 id="dm-top-series" class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink">Largest series</h2></header>
				<table class="w-full text-[11px]">
					<tbody>
						{#each s.top_series as t (t.stream + t.venue + t.symbol + t.timeframe)}
							<tr class="border-b border-sc-line last:border-b-0 hover:bg-sc-ink/[0.02]">
								<td class="px-3 py-1"><a href={seriesHref(t)} class="text-sc-ink hover:underline">{t.symbol.replace('-', '/')}</a> <span class="font-mono text-sc-ink2">{t.timeframe}</span></td>
								<td class="px-2 py-1 text-[10px] text-sc-ink3">{streamLabel(t.stream)}{t.venue !== 'canonical' ? ` · ${t.venue}` : ''}</td>
								<td class="px-3 py-1 text-right font-mono text-[10px] tabular-nums text-sc-ink">{formatBytes(t.bytes)}</td>
							</tr>
						{/each}
					</tbody>
				</table>
			</section>
		</div>

		<section class="rounded-md border border-sc-line bg-sc-panel" aria-labelledby="dm-reclaim">
			<header class="flex items-center gap-2 border-b border-sc-line px-3 py-1.5">
				<h2 id="dm-reclaim" class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink">Reclaimable space</h2>
				<span class="text-[10px] text-sc-ink3">moves to the trash first; nothing is deleted until the trash is emptied</span>
			</header>
			{#each s.reclaimable as group (group.kind)}
				<div class="border-b border-sc-line last:border-b-0" data-testid="reclaim-{group.kind}">
					<div class="flex flex-wrap items-start gap-x-3 gap-y-1 px-3 py-2">
						<div class="min-w-0 flex-1">
							<div class="flex flex-wrap items-baseline gap-2">
								<span class="text-[12px] text-sc-ink">{group.label}</span>
								<span class="font-mono text-[11px] tabular-nums text-sc-ink">{formatBytes(group.bytes)}</span>
								<span class="text-[10px] text-sc-ink3">{plural(group.count, group.kind === 'empty_dirs' || group.kind === 'stray_dirs' ? 'folder' : 'item')}</span>
								<span class="border px-1 font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] {group.safe ? 'border-emerald-900 text-emerald-400' : 'border-amber-900 text-amber-400'}"
									title={group.safe ? 'Reclaiming cannot lose data a series needs' : 'Look at the items before reclaiming them'}>{group.safe ? 'Safe' : 'Review first'}</span>
							</div>
							<p class="mt-0.5 text-[11px] leading-relaxed text-sc-ink2">{group.description}</p>
						</div>
						<div class="flex shrink-0 gap-1.5">
							{#if group.items.length}
								<button type="button" on:click={() => (expanded = { ...expanded, [group.kind]: !expanded[group.kind] })} aria-expanded={!!expanded[group.kind]}
									class="rounded-md border border-sc-line2 px-2 py-0.5 text-[12px] text-sc-ink2 hover:border-sc-ink hover:text-sc-ink">{expanded[group.kind] ? 'Hide items' : 'Show items'}</button>
							{/if}
							<button type="button" on:click={() => (confirming = group.kind)} disabled={!group.count || reclaiming}
								class="rounded-md border border-sc-line2 px-2 py-0.5 text-[12px] text-sc-ink hover:border-red-500 hover:text-red-400 disabled:opacity-30">{group.kind === 'revisions' ? 'Prune' : 'Move to trash'}</button>
						</div>
					</div>
					{#if expanded[group.kind]}
						<div class="max-h-56 overflow-y-auto border-t border-sc-line bg-sc-bg/40">
							<table class="w-full text-[10px]">
								<tbody>
									{#each group.items as item (item.id)}
										<tr class="border-b border-sc-line">
											<td class="px-3 py-0.5 font-mono text-sc-ink2" title={item.note ?? item.path}>{item.path}</td>
											<td class="px-2 py-0.5 text-right font-mono tabular-nums text-sc-ink2">{formatBytes(item.bytes)}</td>
											<td class="px-3 py-0.5 text-right text-sc-ink3" title={formatUtc(item.modified_at)}>{item.modified_at ? formatUtc(item.modified_at, { date: true }) : ''}</td>
										</tr>
									{/each}
								</tbody>
							</table>
							{#if group.count > group.items.length}<p class="px-3 py-1 text-[10px] text-sc-ink3">and {formatCount(group.count - group.items.length)} more</p>{/if}
						</div>
					{/if}
					{#if confirming === group.kind}
						<div class="border-t border-red-950 bg-[#070303] px-3 py-2">
							<TypedConfirm phrase="reclaim {group.kind}" action={group.kind === 'revisions' ? 'Prune' : 'Move to trash'} busy={reclaiming}
								on:confirm={() => reclaim(group)} on:cancel={() => (confirming = null)}>
								<p class="text-[11px] text-sc-ink2">
									{#if group.kind === 'revisions'}
										Prunes {formatBytes(group.bytes)} of revision history older than {s.revisions.keep_days} days. Restatements inside a saved verdict’s window are kept.
									{:else}
										Moves {plural(group.count, 'item')} ({formatBytes(group.bytes)}) to the trash. You can restore them for {s.trash.retention_days} days.
									{/if}
								</p>
							</TypedConfirm>
						</div>
					{/if}
				</div>
			{:else}
				<p class="px-3 py-4 text-[11px] text-sc-ink3">Nothing to reclaim.</p>
			{/each}
		</section>
	{/if}

	<section class="rounded-md border border-sc-line bg-sc-panel" aria-labelledby="dm-trash">
		<header class="flex items-center gap-2 border-b border-sc-line px-3 py-1.5">
			<h2 id="dm-trash" class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink">Trash</h2>
			{#if trash.data}<span class="text-[10px] text-sc-ink3">{formatBytes(trash.data.bytes)} · purged automatically after {trash.data.retention_days} days</span>{/if}
			{#if trash.data?.items.length}
				<button type="button" on:click={() => (confirmPurge = true)} class="rounded-md ml-auto border border-sc-line2 px-2 py-0.5 text-[12px] text-sc-ink2 hover:border-red-500 hover:text-red-400">Empty now</button>
			{/if}
		</header>
		<SectionState state={trash} what="The trash" endpoint="GET /api/data/trash" rows={2} on:retry={loadTrash}>
			{#if confirmPurge && trash.data}
				<div class="border-b border-red-950 bg-[#070303] px-3 py-2">
					<TypedConfirm phrase="empty trash" action="Delete for good" busy={purging} on:confirm={purge} on:cancel={() => (confirmPurge = false)}>
						<p class="text-[11px] text-red-300">Permanently deletes {formatCount(trash.data.items.length)} items ({formatBytes(trash.data.bytes)}). This cannot be undone.</p>
					</TypedConfirm>
				</div>
			{/if}
			{#each trash.data?.items ?? [] as item (item.id)}
				<div class="flex flex-wrap items-center gap-x-3 gap-y-0.5 border-b border-sc-line px-3 py-1.5 text-[11px] last:border-b-0">
					<span class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">{item.kind}</span>
					<span class="text-sc-ink">{item.label}</span>
					<span class="font-mono text-[10px] text-sc-ink2">{formatBytes(item.bytes)}</span>
					<span class="text-[10px] text-sc-ink3" title={formatUtc(item.deleted_at)}>{item.reason} · {formatRelative(item.deleted_at, $clock)}</span>
					<span class="text-[10px] text-sc-ink3" title={formatUtc(item.purge_after)}>purged {purgeIn(item.purge_after, $clock)}</span>
					<button type="button" on:click={() => restore(item.id, item.label)} disabled={restoring[item.id]}
						class="rounded-md ml-auto border border-sc-line2 px-2 py-0.5 text-[12px] text-sc-ink hover:border-sc-ink hover:text-sc-ink disabled:opacity-40">{restoring[item.id] ? 'Restoring…' : 'Restore'}</button>
				</div>
			{:else}
				<p class="px-3 py-3 text-[11px] text-sc-ink3">The trash is empty.</p>
			{/each}
		</SectionState>
	</section>

	<section class="rounded-md border border-sc-line bg-sc-panel" aria-labelledby="dm-audit">
		<header class="flex items-center gap-2 border-b border-sc-line px-3 py-1.5">
			<h2 id="dm-audit" class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink">Identity audit</h2>
			<span class="text-[10px] text-sc-ink3">a report: suggestions only, nothing is moved</span>
		</header>
		<SectionState state={audit} what="The identity audit" endpoint="GET /api/data/identity/audit" rows={3} on:retry={loadAudit}>
			{#each audit.data?.issues ?? [] as issue (issue.kind + issue.path)}
				<div class="border-b border-sc-line px-3 py-2 text-[11px] last:border-b-0">
					<div class="flex flex-wrap items-baseline gap-2">
						<span class="border border-sc-line2 px-1 font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink2">{AUDIT_LABEL[issue.kind] ?? issue.kind}</span>
						<span class="font-mono text-sc-ink">{issue.path}</span>
						{#if issue.bytes}<span class="font-mono text-[10px] text-sc-ink3">{formatBytes(issue.bytes)}</span>{/if}
					</div>
					<p class="mt-0.5 text-sc-ink2">{issue.detail}</p>
					{#if issue.related.length}<p class="text-[10px] text-sc-ink3">Related: <span class="font-mono">{issue.related.join(', ')}</span></p>{/if}
					<p class="text-[10px] text-sky-300/80">Suggestion: {issue.suggestion}</p>
				</div>
			{:else}
				<p class="px-3 py-3 text-[11px] text-sc-ink3">No identity issues found.</p>
			{/each}
		</SectionState>
	</section>
</div>
