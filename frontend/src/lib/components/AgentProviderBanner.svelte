<script lang="ts">
	import { onDestroy, onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { getAgentProviderHealth } from '$lib/api';

	interface ProviderWarning {
		agent_id: string;
		provider: string;
		fallback: string | null;
	}

	let warnings: ProviderWarning[] = [];
	let dismissed = false;
	let pollTimer: ReturnType<typeof setInterval> | null = null;

	const POLL_MS = 60_000;
	const SESSION_KEY = 'forven.agent_provider_banner.dismissed';

	async function refresh() {
		if (dismissed) return;
		try {
			const res = await getAgentProviderHealth();
			warnings = res?.warnings ?? [];
		} catch {
			// Best-effort hint; leave the prior state on error.
		}
	}

	function handleDismiss() {
		dismissed = true;
		try {
			sessionStorage.setItem(SESSION_KEY, '1');
		} catch {
			// sessionStorage unavailable; dismiss for the lifetime of this mount.
		}
	}

	function openAgents() {
		goto('/agents');
	}

	onMount(() => {
		try {
			dismissed = sessionStorage.getItem(SESSION_KEY) === '1';
		} catch {
			dismissed = false;
		}
		void refresh();
		pollTimer = setInterval(() => void refresh(), POLL_MS);
	});

	onDestroy(() => {
		if (pollTimer !== null) {
			clearInterval(pollTimer);
			pollTimer = null;
		}
	});

	$: visible = !dismissed && warnings.length > 0;
	$: providerList = Array.from(new Set(warnings.map((w) => w.provider))).join(', ');
	$: fallback = warnings.find((w) => w.fallback)?.fallback ?? null;
</script>

{#if visible}
	<div
		class="border-b border-yellow-900 bg-yellow-500/5 text-yellow-400 px-4 py-2 flex items-center justify-between gap-3"
		role="status"
	>
		<div class="flex items-center gap-3 min-w-0">
			<svg class="w-4 h-4 text-yellow-400 shrink-0" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
				<path d="M12 2L1 21h22L12 2zm0 4.83L19.53 19H4.47L12 6.83zM11 10v4h2v-4h-2zm0 6v2h2v-2h-2z" />
			</svg>
			<div class="text-[11px] leading-snug min-w-0">
				<span class="font-bold">{warnings.length} agent{warnings.length === 1 ? '' : 's'}</span>
				use provider <span class="font-semibold">{providerList}</span> with no credentials{#if fallback}, so
				tasks fall back to <span class="font-semibold">{fallback}</span>{/if}. Set each agent's model
				in Settings → Agents, or add the provider's API key.
			</div>
		</div>
		<div class="flex items-center gap-2 shrink-0">
			<button
				type="button"
				class="text-[11px] border border-yellow-900 text-yellow-400 px-2.5 py-1 hover:bg-yellow-500/10 transition-colors"
				on:click={openAgents}
			>
				Open Agents
			</button>
			<button
				type="button"
				class="text-[11px] text-[#666] hover:text-white px-2 transition-colors"
				on:click={handleDismiss}
				aria-label="Dismiss"
			>
				✕
			</button>
		</div>
	</div>
{/if}
