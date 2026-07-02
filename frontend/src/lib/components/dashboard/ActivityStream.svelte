<script lang="ts">
	import { goto } from '$app/navigation';
	import type { DashboardActivityItem } from '$lib/api';
	import StrategyLink from '$lib/components/ui/StrategyLink.svelte';

	export let items: DashboardActivityItem[] = [];

	function fmtTs(ts: string | number | null | undefined): string {
		if (ts === null || ts === undefined) return '—';
		let date: Date;
		if (typeof ts === 'number') {
			date = new Date(ts);
		} else {
			const trimmed = ts.trim();
			if (!trimmed) return '—';
			const asEpoch = /^\d+$/.test(trimmed) ? Number(trimmed) : Number.NaN;
			date = Number.isFinite(asEpoch) ? new Date(asEpoch) : new Date(trimmed);
		}
		if (Number.isNaN(date.getTime())) return '—';
		return date.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
	}

	function typeColor(type: string): string {
		if (type === 'lifecycle') return 'text-white';
		if (type === 'autopilot_job') return 'text-emerald-400';
		if (type === 'signal') return 'text-[#888]';
		return 'text-[#666]';
	}

	async function openActivity(item: DashboardActivityItem) {
		if (item.strategy_id) {
			await goto(`/lab/strategy/${item.strategy_id}?returnTo=${encodeURIComponent('/')}`);
			return;
		}
		if (item.type === 'signal') {
			await goto('/signals');
			return;
		}
		await goto('/lab?tab=247');
	}

	function activityKey(item: DashboardActivityItem, index: number): string {
		const type = String(item.type ?? 'task');
		const id = String(item.id ?? '').trim();
		if (id) return `${type}:${id}`;
		const ts = String(item.ts ?? '').trim();
		const title = String(item.title ?? '').trim();
		return `${type}:${ts}:${title}:${index}`;
	}
</script>

<div class="terminal-card flex flex-col h-full">
	<div class="border-b border-[#1a1a1a] px-4 py-2 text-[10px] font-bold uppercase tracking-widest text-[#888]">Activity Stream</div>
	<div class="flex-1 overflow-auto">
		{#each items as item, index (activityKey(item, index))}
			<div
				class="w-full text-left px-4 py-2 border-b border-[#111] hover:bg-[#111] transition-colors cursor-pointer"
				role="button"
				tabindex="0"
				on:click={() => openActivity(item)}
				on:keydown={(event) => {
					if (event.key === 'Enter' || event.key === ' ') {
						event.preventDefault();
						void openActivity(item);
					}
				}}
			>
				<div class="flex items-center gap-2 text-[11px]">
					<span class={`font-mono ${typeColor(item.type)}`}>{item.type}</span>
					<span class="text-[#555] ml-auto">{fmtTs(item.ts)}</span>
				</div>
				<div class="text-xs text-white truncate mt-1" title={item.title}>{item.title}</div>
				{#if item.detail}
					<div class="text-[11px] text-[#666] truncate" title={item.detail}>{item.detail}</div>
				{/if}
				{#if item.strategy_id}
					<div class="mt-2">
						<StrategyLink strategyId={item.strategy_id} returnTo="/" />
					</div>
				{/if}
			</div>
		{:else}
			<div class="px-3 py-8 text-center text-xs text-[#555]">No recent activity</div>
		{/each}
	</div>
</div>
