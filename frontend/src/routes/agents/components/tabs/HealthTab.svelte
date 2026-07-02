<script lang="ts">
	/**
	 * Health tab. Polls GET /api/agents/provider-health and renders per-provider
	 * runtime health cards (green/amber/red) plus the existing pinned-credential
	 * `warnings`, with a one-click "Reconcile providers" button.
	 *
	 * Runtime shape (backend being updated to match):
	 *   { provider, state: "ok"|"degraded"|"down",
	 *     kind: "rate_limit"|"quota"|"auth"|"transient"|"fallback",
	 *     message, since, fallback_to }
	 * Every field beyond provider/state is optional — degrade gracefully.
	 */
	import { onDestroy, onMount } from 'svelte';
	import {
		getProviderHealth,
		reconcileAgentProviders,
		type ProviderRuntimeHealth,
		type AgentProviderWarning,
	} from '$lib/api';
	import { addToast } from '$lib/stores/processTracker';

	let runtime: ProviderRuntimeHealth[] = [];
	let warnings: AgentProviderWarning[] = [];
	let loading = true;
	let error: string | null = null;
	let reconciling = false;
	let pollTimer: ReturnType<typeof setInterval> | null = null;

	const POLL_MS = 30_000;

	async function refresh() {
		try {
			const res = await getProviderHealth();
			runtime = res.runtime ?? [];
			warnings = res.warnings ?? [];
			error = null;
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to load provider health';
		} finally {
			loading = false;
		}
	}

	async function reconcile() {
		reconciling = true;
		try {
			const res = await reconcileAgentProviders();
			const moved = res?.updated ?? 0;
			addToast(
				moved > 0
					? `Reconciled ${moved} agent${moved === 1 ? '' : 's'}${res?.provider ? ` onto ${res.provider}` : ''}.`
					: 'Providers reconciled — nothing to change.',
				'success',
			);
			await refresh();
		} catch (e) {
			addToast(e instanceof Error ? e.message : 'Failed to reconcile providers', 'error');
		} finally {
			reconciling = false;
		}
	}

	onMount(() => {
		void refresh();
		pollTimer = setInterval(() => void refresh(), POLL_MS);
	});
	onDestroy(() => {
		if (pollTimer !== null) clearInterval(pollTimer);
		pollTimer = null;
	});

	function stateColor(state: string): string {
		if (state === 'down') return 'border-red-900 bg-red-500/5';
		if (state === 'degraded') return 'border-yellow-900 bg-yellow-500/5';
		if (state === 'ok') return 'border-emerald-900 bg-emerald-500/5';
		return 'border-[#222] bg-[#050505]';
	}
	function dotColor(state: string): string {
		if (state === 'down') return 'bg-red-500';
		if (state === 'degraded') return 'bg-yellow-400';
		if (state === 'ok') return 'bg-emerald-400';
		return 'bg-[#555]';
	}
	function stateLabel(state: string): string {
		if (state === 'down') return 'Down';
		if (state === 'degraded') return 'Degraded';
		if (state === 'ok') return 'OK';
		return state || 'Unknown';
	}
	function formatSince(value?: string | number | null): string {
		if (value === null || value === undefined || value === '') return '';
		// Backend emits epoch SECONDS as a number; an ISO string is also accepted.
		let t: number;
		if (typeof value === 'number') {
			t = value * 1000;
		} else {
			t = Date.parse(value);
		}
		if (Number.isNaN(t)) return '';
		return new Date(t).toLocaleString('en-US', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false });
	}
</script>

<div class="space-y-6">
	<section class="terminal-card p-6 space-y-4">
		<header class="border-b border-[#1a1a1a] pb-2 flex items-start justify-between gap-3">
			<div>
				<h2 class="text-sm font-bold uppercase tracking-widest text-white">Provider health</h2>
				<p class="text-xs text-[#666] mt-1">
					Live per-provider state as observed during agent and Brain calls. Polls every {POLL_MS / 1000}s.
				</p>
			</div>
			<div class="flex items-center gap-2">
				<button
					type="button"
					on:click={refresh}
					disabled={loading}
					class="terminal-button text-xs px-2 py-1 disabled:opacity-60"
				>
					{loading ? 'Refreshing…' : 'Refresh'}
				</button>
				<button
					type="button"
					on:click={reconcile}
					disabled={reconciling}
					class="terminal-button-primary text-xs px-3 py-1 disabled:opacity-60"
					title="Re-point agents pinned to a credential-less provider onto a connected one."
				>
					{reconciling ? 'Reconciling…' : 'Reconcile providers'}
				</button>
			</div>
		</header>

		{#if error}
			<p class="text-xs text-red-400" role="alert">{error}</p>
		{/if}

		{#if loading && runtime.length === 0}
			<p class="text-sm text-[#666]">Loading provider health…</p>
		{:else if runtime.length === 0}
			<p class="text-sm text-[#666]">
				No runtime health reported. Providers report state here once they're exercised by agent/Brain calls.
			</p>
		{:else}
			<div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
				{#each runtime as r (r.provider)}
					<div class="border p-4 space-y-2 {stateColor(r.state)}">
						<div class="flex items-center justify-between gap-2">
							<span class="font-mono text-sm text-white uppercase">{r.provider}</span>
							<span class="flex items-center gap-1.5 text-[11px] uppercase tracking-wider text-[#ccc]">
								<span class="w-1.5 h-1.5 rounded-full {dotColor(r.state)}"></span>
								{stateLabel(r.state)}
							</span>
						</div>
						{#if r.kind}
							<div class="text-[10px] uppercase tracking-wider text-[#888]">{r.kind}</div>
						{/if}
						{#if r.message}
							<p class="text-xs text-[#888]">{r.message}</p>
						{/if}
						{#if r.fallback_to}
							<p class="text-xs text-yellow-400">Falling back to <span class="font-mono">{r.fallback_to}</span></p>
						{/if}
						{#if r.since && formatSince(r.since)}
							<p class="text-[10px] text-[#555]">since {formatSince(r.since)}</p>
						{/if}
					</div>
				{/each}
			</div>
		{/if}
	</section>

	<section class="terminal-card p-6 space-y-3">
		<header class="border-b border-[#1a1a1a] pb-2">
			<h3 class="text-sm font-bold tracking-widest uppercase text-[#888]">Pinned-credential warnings</h3>
			<p class="text-xs text-[#666] mt-1">
				Agents pinned to a provider that has no credentials. Connect the provider or repoint the agent.
			</p>
		</header>
		{#if warnings.length === 0}
			<p class="text-sm text-[#666]">No agents are pinned to a credential-less provider.</p>
		{:else}
			<ul class="space-y-1.5">
				{#each warnings as w (w.agent_id + ':' + w.provider)}
					<li class="flex items-center justify-between gap-2 bg-yellow-500/5 border border-yellow-900 px-3 py-2 text-xs">
						<span class="text-[#ccc]">
							<span class="font-mono">{w.agent_id}</span> → provider <span class="font-mono text-yellow-400">{w.provider}</span>
						</span>
						{#if w.fallback}
							<span class="text-[#888]">falls back to <span class="font-mono text-[#ccc]">{w.fallback}</span></span>
						{:else}
							<span class="text-yellow-400">no fallback</span>
						{/if}
					</li>
				{/each}
			</ul>
		{/if}
	</section>
</div>
