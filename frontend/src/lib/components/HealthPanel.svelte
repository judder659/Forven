<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import { getHealthStatus } from '$lib/api/forven';
	import type { HealthStatusResponse, ComponentStatus, HealthDataCheck } from '$lib/api/types';

	let healthData: HealthStatusResponse | null = null;
	let expanded = false;
	let error: string | null = null;
	let pollTimer: ReturnType<typeof setInterval> | null = null;

	// Restore collapse state from localStorage
	onMount(() => {
		try {
			expanded = localStorage.getItem('health_panel_expanded') === 'true';
		} catch { /* ignore */ }
		fetchHealth();
		pollTimer = setInterval(fetchHealth, 10_000);
	});

	onDestroy(() => {
		if (pollTimer) clearInterval(pollTimer);
	});

	async function fetchHealth() {
		try {
			healthData = await getHealthStatus();
			error = null;
		} catch (e) {
			error = 'Health monitor unavailable';
		}
	}

	function toggleExpanded() {
		expanded = !expanded;
		try {
			localStorage.setItem('health_panel_expanded', String(expanded));
		} catch { /* ignore */ }
	}

	function stateColor(state: string): string {
		switch (state) {
			case 'green': return 'bg-emerald-400';
			case 'amber': return 'bg-yellow-400';
			case 'red': return 'bg-red-400';
			default: return 'bg-[#555]';
		}
	}

	function stateTextColor(state: string): string {
		switch (state) {
			case 'green': return 'text-emerald-400';
			case 'amber': return 'text-yellow-400';
			case 'red': return 'text-red-400';
			default: return 'text-[#888]';
		}
	}

	function overallLabel(state: string): string {
		switch (state) {
			case 'green': return 'Healthy';
			case 'amber': return 'Degraded';
			case 'red': return 'Critical';
			default: return 'Unknown';
		}
	}

	function formatLastSeen(ts: string | null): string {
		if (!ts) return 'never';
		try {
			const dt = new Date(ts);
			const age = (Date.now() - dt.getTime()) / 1000;
			if (age < 60) return `${Math.round(age)}s ago`;
			if (age < 3600) return `${Math.round(age / 60)}m ago`;
			return `${Math.round(age / 3600)}h ago`;
		} catch {
			return 'unknown';
		}
	}

	function friendlyName(name: string): string {
		return name
			.replace(/^bot:/, '')
			.replace(/_/g, ' ')
			.replace(/\b\w/g, (c) => c.toUpperCase());
	}

	$: components = healthData?.components ?? [];
	$: dataChecks = healthData?.data_checks ?? [];
	$: overall = healthData?.overall ?? 'green';
	$: monitorRunning = healthData?.monitor_running ?? false;
	$: failedChecks = dataChecks.filter((d) => !d.passed);
</script>

{#if healthData || error}
<div class="mb-4 border border-[#222] bg-[#050505]">
	<!-- Header bar — always visible -->
	<button
		type="button"
		class="w-full flex items-center gap-3 px-4 py-2.5 text-left hover:bg-[#111] transition-colors"
		on:click={toggleExpanded}
	>
		<span class={`inline-flex h-2.5 w-2.5 rounded-full ${stateColor(overall)} ${overall === 'red' ? 'animate-pulse' : ''}`}></span>
		<span class="text-[11px] uppercase tracking-[0.15em] text-[#888] font-medium">System Health</span>
		<span class={`text-[11px] font-semibold ${stateTextColor(overall)}`}>{overallLabel(overall)}</span>

		{#if !monitorRunning && !error}
			<span class="text-[10px] text-[#666] ml-1">(starting...)</span>
		{/if}

		{#if failedChecks.length > 0}
			<span class="ml-auto text-[10px] px-1.5 py-0.5 border border-red-900 bg-red-500/10 text-red-400">
				{failedChecks.length} issue{failedChecks.length !== 1 ? 's' : ''}
			</span>
		{/if}

		<svg class={`w-3.5 h-3.5 text-[#555] ml-auto transition-transform ${expanded ? 'rotate-180' : ''}`} fill="none" stroke="currentColor" viewBox="0 0 24 24">
			<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
		</svg>
	</button>

	{#if expanded}
	<div class="px-4 pb-3 border-t border-[#1a1a1a]">
		{#if error}
			<p class="text-xs text-[#666] py-2">{error}</p>
		{:else}
			<!-- Row 1: Service status pills -->
			<div class="flex flex-wrap gap-2 py-3">
				{#each components as comp (comp.name)}
					<div
						class="inline-flex items-center gap-1.5 px-2.5 py-1 border border-[#222] bg-[#111] text-[11px]"
						title="{comp.message}\nLast seen: {formatLastSeen(comp.last_seen)}"
					>
						<span class={`inline-flex h-1.5 w-1.5 rounded-full ${stateColor(comp.state)}`}></span>
						<span class="text-[#888]">{friendlyName(comp.name)}</span>
						<span class="text-[#666] text-[10px]">{formatLastSeen(comp.last_seen)}</span>
					</div>
				{/each}
				{#if components.length === 0}
					<span class="text-xs text-[#666]">No components registered yet</span>
				{/if}
			</div>

			<!-- Row 2: Data integrity summary -->
			{#if dataChecks.length > 0}
				<div class="flex flex-wrap gap-x-4 gap-y-1 py-2 border-t border-[#1a1a1a] text-[11px]">
					{#each dataChecks as check (check.name)}
						<span class={check.passed ? 'text-[#666]' : check.severity === 'critical' ? 'text-red-400' : 'text-yellow-400'}>
							{check.name.replace(/_/g, ' ')}: {check.detail}
						</span>
					{/each}
				</div>
			{/if}
		{/if}
	</div>
	{/if}
</div>
{/if}
