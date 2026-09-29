<script lang="ts">
	import { createEventDispatcher } from 'svelte';
	import { formatRegimeLabel, regimeBadgeClass } from '$lib/utils/labRegime';

	export let regime = '';
	export let confidence = 0;
	export let uncertain = false;
	export let championId: string | null = null;
	export let lastCycleAt: string | null = null;
	export let running = false;

	const dispatch = createEventDispatcher<{ run: void }>();

	function formatRelative(iso: string | null): string {
		if (!iso) return 'Never';
		const diff = Date.now() - new Date(iso).getTime();
		if (diff < 0) return 'Just now';
		const mins = Math.floor(diff / 60_000);
		if (mins < 1) return 'Just now';
		if (mins < 60) return `${mins}m ago`;
		const hrs = Math.floor(mins / 60);
		if (hrs < 24) return `${hrs}h ago`;
		return `${Math.floor(hrs / 24)}d ago`;
	}

	$: regimeKey = (regime || '').trim().toUpperCase();
	$: badgeClass = regimeKey
		? regimeBadgeClass({ regime: regimeKey, uncertain })
		: 'border-sc-line2 bg-sc-panel2 text-sc-ink2';
</script>

<div class="rounded-md flex flex-wrap items-center justify-between gap-3 border border-sc-line bg-sc-panel px-4 py-3">
	<div class="flex flex-wrap items-center gap-4">
		<!-- Current regime -->
		<div class="flex items-center gap-2">
			<span class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Regime</span>
			{#if regimeKey}
				<span class={`inline-flex border px-2.5 py-0.5 font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] font-medium ${badgeClass}`}>
					{formatRegimeLabel({ regime: regimeKey })}
				</span>
				{#if uncertain}
					<span class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-yellow-400">uncertain</span>
				{/if}
			{:else}
				<span class="text-sm text-sc-ink3">—</span>
			{/if}
		</div>

		<!-- Confidence -->
		{#if confidence > 0}
			<div class="flex items-center gap-1.5">
				<span class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Conf</span>
				<span class={`text-sm font-medium ${uncertain ? 'text-yellow-400' : 'text-sc-ink'}`}>
					{(confidence * 100).toFixed(0)}%
				</span>
			</div>
		{/if}

		<!-- Champion -->
		<div class="flex items-center gap-1.5">
			<span class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Champion</span>
			<span class={`text-sm font-medium ${championId ? 'text-emerald-400' : 'text-sc-ink3'}`}>
				{championId || 'None'}
			</span>
		</div>

		<!-- Last cycle -->
		<div class="flex items-center gap-1.5">
			<span class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Last cycle</span>
			<span class={`text-sm ${running ? 'text-emerald-400' : 'text-sc-ink2'}`}>
				{running ? 'Running…' : formatRelative(lastCycleAt)}
			</span>
		</div>
	</div>

	<button
		class="rounded-md border px-4 py-2 text-sm font-medium transition-colors
			{running
				? 'cursor-not-allowed border-sc-line2 bg-sc-panel2 text-sc-ink3'
				: 'border-sc-ink text-sc-ink hover:bg-sc-panel2'}"
		disabled={running}
		on:click={() => dispatch('run')}
	>
		{running ? 'Running…' : 'Run New Cycle'}
	</button>
</div>
