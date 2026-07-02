<script lang="ts">
	import type { HypothesisSummary } from '$lib/api';
	import {
		crucibleStatusClasses,
		crucibleStatusLabel,
		managerStateLabel,
		originClasses,
		originLabel,
		protectionBadge,
	} from '$lib/crucible';
	import SourceTags from './SourceTags.svelte';

	export let hypotheses: HypothesisSummary[] = [];
	export let loading = false;
	export let emptyMessage = 'No crucibles available yet.';
	export let selectedIds: Set<string> = new Set();
	export let mutationPending = false;
	export let onToggleSelect: (hypothesisId: string) => void = () => {};
	export let onToggleSelectAll: () => void = () => {};
	export let onArchive: (hypothesisId: string) => void | Promise<void> = () => {};
	export let onTrash: (hypothesisId: string) => void | Promise<void> = () => {};
	export let onRestore: (hypothesisId: string) => void | Promise<void> = () => {};
	export let onResearch: (hypothesisId: string) => void | Promise<void> = () => {};

	function formatScore(value: number | null | undefined): string {
		const numeric = Number(value ?? 0);
		return `${Math.round(numeric * 100)}%`;
	}

	function formatPercent(value: number | null | undefined, decimals = 1): string {
		if (value === null || value === undefined || !Number.isFinite(value)) return '—';
		return `${value.toFixed(decimals)}%`;
	}

	function formatNumber(value: number | null | undefined, decimals = 2): string {
		if (value === null || value === undefined || !Number.isFinite(value)) return '—';
		return value.toFixed(decimals);
	}

	function formatStamp(value: string | null | undefined): string {
		if (!value) return 'N/A';
		const normalized = value.includes('T') ? value : `${value}Z`;
		const parsed = new Date(normalized);
		if (Number.isNaN(parsed.getTime())) return value;
		return parsed.toLocaleString(undefined, {
			month: 'short',
			day: 'numeric',
			hour: 'numeric',
			minute: '2-digit',
		});
	}

	function taskLabel(type: string | undefined): string {
		if (!type) return 'Task active';
		return type.replace(/_/g, ' ').replace(/\b\w/g, (char) => char.toUpperCase());
	}

	function bestResultSummary(hypothesis: HypothesisSummary): string {
		if (hypothesis.best_outcome) {
			const outcome = hypothesis.best_outcome;
			return [
				`Sharpe ${formatNumber(outcome.sharpe, 2)}`,
				`Return ${formatPercent(outcome.total_return_pct, 1)}`,
				`${formatNumber(outcome.total_trades, 0)} trades`,
			].join(' / ');
		}
		return hypothesis.best_result ?? '';
	}

	function formatScopeList(value: unknown, fallback: string): string {
		if (Array.isArray(value)) {
			const rawItems = value.map((item) => String(item));
			if (rawItems.length > 5 && rawItems.every((item) => item.length <= 1)) {
				const rebuilt = rawItems.join('').trim();
				return rebuilt || fallback;
			}
			const cleaned = rawItems.map((item) => item.trim()).filter(Boolean);
			return cleaned.length ? cleaned.join(', ') : fallback;
		}
		if (typeof value === 'string') {
			const cleaned = value.trim();
			return cleaned || fallback;
		}
		return fallback;
	}

	$: allSelected = hypotheses.length > 0 && hypotheses.every((item) => selectedIds.has(item.id));
</script>

<div class="bg-[#050505]">
	<div class="overflow-x-auto">
		<table class="w-full min-w-[1180px] border-collapse">
			<thead class="sticky top-0 z-10 bg-[#050505] text-left text-[10px] uppercase tracking-wider text-[#666]">
				<tr class="border-b border-[#222]">
					<th class="w-12 px-4 py-3">
						<input
							type="checkbox"
							checked={allSelected}
							on:change={() => onToggleSelectAll()}
							aria-label="Select visible crucibles"
							class="h-4 w-4 border border-[#333] bg-[#050505] text-white"
						/>
					</th>
					<th class="px-4 py-3">Crucible</th>
					<th class="px-4 py-3">Stage</th>
					<th class="px-4 py-3">Origin</th>
					<th class="px-4 py-3">Scope</th>
					<th class="px-4 py-3 text-right">Novelty</th>
					<th class="px-4 py-3 text-right">Work</th>
					<th class="px-4 py-3 text-right">Best</th>
					<th class="px-4 py-3 text-right">Data Gaps</th>
					<th class="px-4 py-3">Updated</th>
					<th class="px-4 py-3 text-right">Actions</th>
				</tr>
			</thead>
			<tbody class="bg-[#050505] text-sm text-white">
				{#if loading}
					{#each Array(4) as _, index}
						<tr class="border-b border-[#111]" data-skeleton-index={index}>
							<td colspan="11" class="px-4 py-4">
								<div class="h-10 animate-pulse bg-[#111]"></div>
							</td>
						</tr>
					{/each}
				{:else if hypotheses.length === 0}
					<tr>
						<td colspan="11" class="px-4 py-10 text-center text-sm text-[#666]">{emptyMessage}</td>
					</tr>
				{:else}
					{#each hypotheses as hypothesis}
						{@const hypothesisHrefId = hypothesis.display_id || hypothesis.id}
						{@const protBadge = protectionBadge(hypothesis.protection_status)}
						{@const stage = hypothesis.crucible_status || hypothesis.status || 'proposed'}
						<tr
							class:border-b={true}
							class:border-[#111]={true}
							class:bg-[#111]={selectedIds.has(hypothesis.id)}
							class:text-[#666]={hypothesis.manager_state !== 'active'}
						>
							<td class="px-4 py-3 align-top">
								<input
									type="checkbox"
									data-row-select={hypothesis.id}
									checked={selectedIds.has(hypothesis.id)}
									on:change={() => onToggleSelect(hypothesis.id)}
									class="mt-1 h-4 w-4 border border-[#333] bg-[#050505] text-white"
								/>
							</td>
							<td class="px-4 py-3 align-top">
								<a
									href={`/hypotheses/${encodeURIComponent(hypothesisHrefId)}`}
									class={`font-semibold transition-colors hover:text-white ${
										hypothesis.status === 'disproven'
											? 'text-[#555] line-through'
											: 'text-white'
									}`}
								>
									{hypothesis.title}
								</a>
								<div class="mt-1 flex flex-wrap items-center gap-2 text-[10px] uppercase tracking-wider text-[#666]">
									{#if hypothesis.display_id}
										<span>{hypothesis.display_id}</span>
									{/if}
									<span>{hypothesis.source_type}</span>
									<span>{managerStateLabel(hypothesis.manager_state)}</span>
									{#if hypothesis.quality === 'placeholder'}
										<span class="text-yellow-400">needs research</span>
									{/if}
								</div>
								{#if hypothesis.source_tags?.length}
									<div class="mt-2">
										<SourceTags tags={hypothesis.source_tags} size="sm" />
									</div>
								{/if}
								{#if hypothesis.active_task}
									<div class="mt-2 inline-flex max-w-full items-center gap-1.5 border border-emerald-900 bg-emerald-500/10 px-2 py-1 text-[10px] uppercase tracking-wider text-emerald-400">
										<span class="h-1.5 w-1.5 animate-pulse rounded-full bg-emerald-400"></span>
										<span class="truncate">{taskLabel(hypothesis.active_task.type)}</span>
									</div>
								{/if}
							</td>
							<td class="px-4 py-3 align-top" data-crucible-status={stage} data-verdict={hypothesis.status}>
								<div class="flex flex-col items-start gap-1">
									<span class={`border px-2 py-0.5 text-[10px] uppercase tracking-widest ${crucibleStatusClasses(stage)}`}>
										{crucibleStatusLabel(stage)}
									</span>
									{#if protBadge}
										<span class={`border px-2 py-0.5 text-[9px] uppercase tracking-wider ${protBadge.classes}`}>
											{protBadge.label}
										</span>
									{/if}
								</div>
							</td>
							<td class="px-4 py-3 align-top">
								<span class={`inline-flex items-center border px-2 py-1 text-[10px] font-bold uppercase tracking-wider ${originClasses(hypothesis.origin)}`}>
									{originLabel(hypothesis.origin)}
								</span>
							</td>
							<td class="max-w-60 px-4 py-3 align-top text-xs leading-5 text-[#888]">
								<div class="break-words">{formatScopeList(hypothesis.target_assets, 'No assets')}</div>
								<div class="break-words text-[#666]">{formatScopeList(hypothesis.target_timeframes, 'No timeframes')}</div>
							</td>
							<td class="px-4 py-3 text-right align-top text-sm text-white">{formatScore(hypothesis.novelty_score)}</td>
							<td class="px-4 py-3 text-right align-top">
								<div class="text-sm text-white">{hypothesis.strategy_count}</div>
								{#if hypothesis.active_task}
									<div class="text-[10px] uppercase tracking-wider text-emerald-400">{hypothesis.active_task.status}</div>
								{:else}
									<div class="text-[10px] uppercase tracking-wider text-[#555]">idle</div>
								{/if}
							</td>
							<td class="px-4 py-3 text-right align-top text-xs text-[#888]">
								{#if bestResultSummary(hypothesis)}
									{bestResultSummary(hypothesis)}
								{:else}
									<span class="text-[#555]">—</span>
								{/if}
							</td>
							<td class="px-4 py-3 text-right align-top text-sm {hypothesis.open_data_gap_count > 0 ? 'text-yellow-400' : 'text-[#666]'}">{hypothesis.open_data_gap_count}</td>
							<td class="px-4 py-3 align-top text-xs text-[#666]">{formatStamp(hypothesis.updated_at)}</td>
							<td class="px-4 py-3 align-top">
								<div class="flex flex-wrap justify-end gap-2 text-[11px] font-semibold uppercase tracking-wider">
									<a
										href={`/hypotheses/${encodeURIComponent(hypothesisHrefId)}`}
										class="border border-[#333] px-2 py-1 text-[#888] transition-colors hover:border-white hover:text-white"
									>
										Open
									</a>
									{#if hypothesis.manager_state === 'active'}
										{#if hypothesis.quality === 'placeholder' || hypothesis.quality === 'enriched'}
											<button
												type="button"
												data-row-action={`research-${hypothesis.id}`}
												class="border border-[#333] px-2 py-1 text-[#888] transition-colors hover:border-white hover:text-white disabled:opacity-50"
												on:click={() => onResearch(hypothesis.id)}
												disabled={mutationPending}
												title="Re-queue strategy-developer research task"
											>
												Re-research
											</button>
										{/if}
										<button
											type="button"
											data-row-action={`archive-${hypothesis.id}`}
											class="border border-[#333] px-2 py-1 text-[#888] transition-colors hover:border-white hover:text-white disabled:opacity-50"
											on:click={() => onArchive(hypothesis.id)}
											disabled={mutationPending}
										>
											Archive
										</button>
										<button
											type="button"
											data-row-action={`trash-${hypothesis.id}`}
											class="border border-yellow-900 px-2 py-1 text-yellow-400 transition-colors hover:border-yellow-400 hover:text-yellow-300 disabled:opacity-50"
											on:click={() => onTrash(hypothesis.id)}
											disabled={mutationPending}
										>
											Delete
										</button>
									{:else if hypothesis.manager_state === 'archived'}
										<button
											type="button"
											data-row-action={`restore-${hypothesis.id}`}
											class="border border-[#333] px-2 py-1 text-[#888] transition-colors hover:border-white hover:text-white disabled:opacity-50"
											on:click={() => onRestore(hypothesis.id)}
											disabled={mutationPending}
										>
											Restore
										</button>
										<button
											type="button"
											data-row-action={`trash-${hypothesis.id}`}
											class="border border-yellow-900 px-2 py-1 text-yellow-400 transition-colors hover:border-yellow-400 hover:text-yellow-300 disabled:opacity-50"
											on:click={() => onTrash(hypothesis.id)}
											disabled={mutationPending}
										>
											Delete
										</button>
									{:else}
										<button
											type="button"
											data-row-action={`restore-${hypothesis.id}`}
											class="border border-[#333] px-2 py-1 text-[#888] transition-colors hover:border-white hover:text-white disabled:opacity-50"
											on:click={() => onRestore(hypothesis.id)}
											disabled={mutationPending}
										>
											Restore
										</button>
									{/if}
								</div>
							</td>
						</tr>
					{/each}
				{/if}
			</tbody>
		</table>
	</div>
</div>
