<script lang="ts">
	/**
	 * Global connection-health banner. Mounted in the app shell so it shows on
	 * every page. Driven from GET /api/agents/provider-health `runtime`:
	 *  - RED & non-dismissible when any provider state is "down" with kind
	 *    "auth" or "quota" (the operator MUST act).
	 *  - AMBER & dismissible for "degraded" / "fallback".
	 * When a `fallback_to` is present it's stated explicitly. Links to the Health
	 * tab. Polls ~30s, mirroring AgentProviderBanner.
	 */
	import { onDestroy, onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { getProviderHealth, type ProviderRuntimeHealth } from '$lib/api';

	let runtime: ProviderRuntimeHealth[] = [];
	let dismissed = false;
	let pollTimer: ReturnType<typeof setInterval> | null = null;

	const POLL_MS = 30_000;
	const SESSION_KEY = 'forven.connection_health_banner.dismissed';

	async function refresh() {
		try {
			const res = await getProviderHealth();
			runtime = res.runtime ?? [];
		} catch {
			// Best-effort hint; leave the prior state on error.
		}
	}

	function isCritical(r: ProviderRuntimeHealth): boolean {
		return r.state === 'down' && (r.kind === 'auth' || r.kind === 'quota');
	}
	function isWarning(r: ProviderRuntimeHealth): boolean {
		return !isCritical(r) && (r.state === 'degraded' || r.state === 'down' || r.kind === 'fallback');
	}

	$: critical = runtime.filter(isCritical);
	$: warnings = runtime.filter(isWarning);
	// Critical alerts are non-dismissible. Warnings honor the session dismiss.
	$: showCritical = critical.length > 0;
	$: showWarning = !showCritical && !dismissed && warnings.length > 0;
	$: active = showCritical ? critical : warnings;

	function describe(r: ProviderRuntimeHealth): string {
		const subject = r.fallback_to === null || r.fallback_to === undefined ? `${r.provider}` : `${r.provider}`;
		const reason = r.kind ? ` (${r.kind})` : '';
		const base = r.message?.trim()
			? r.message.trim()
			: `${subject} ${r.state === 'down' ? 'failed' : 'is degraded'}${reason}`;
		if (r.fallback_to) {
			return `${base} — falling back to ${r.fallback_to}.`;
		}
		// Make the "no fallback configured / fail closed" case explicit.
		if (r.state === 'down') {
			return `${base} — no fallback configured.`;
		}
		return base;
	}

	function handleDismiss() {
		dismissed = true;
		try {
			sessionStorage.setItem(SESSION_KEY, '1');
		} catch {
			// sessionStorage unavailable; dismiss for the lifetime of this mount.
		}
	}

	function openHealth() {
		goto('/agents?tab=health');
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
</script>

{#if showCritical || showWarning}
	<div
		class="border-b px-4 py-2 flex items-center justify-between gap-3 {showCritical
			? 'border-red-900 bg-red-500/5 text-red-400'
			: 'border-yellow-900 bg-yellow-500/5 text-yellow-400'}"
		role={showCritical ? 'alert' : 'status'}
	>
		<div class="flex items-center gap-3 min-w-0">
			<svg
				class="w-4 h-4 shrink-0 {showCritical ? 'text-red-400' : 'text-yellow-400'}"
				viewBox="0 0 24 24"
				fill="currentColor"
				aria-hidden="true"
			>
				<path d="M12 2L1 21h22L12 2zm0 4.83L19.53 19H4.47L12 6.83zM11 10v4h2v-4h-2zm0 6v2h2v-2h-2z" />
			</svg>
			<div class="text-[11px] leading-snug min-w-0">
				{#if active.length === 1}
					<span class="font-bold uppercase mr-1">{showCritical ? 'Action required' : 'Degraded'}:</span>
					{describe(active[0])}
				{:else}
					<span class="font-bold">{active.length} providers</span>
					{showCritical ? 'need attention' : 'degraded'}:
					{active.map(describe).join(' ')}
				{/if}
			</div>
		</div>
		<div class="flex items-center gap-2 shrink-0">
			<button
				type="button"
				class="text-[11px] border px-2.5 py-1 transition-colors {showCritical
					? 'border-red-900 text-red-400 hover:bg-red-500/10'
					: 'border-yellow-900 text-yellow-400 hover:bg-yellow-500/10'}"
				on:click={openHealth}
			>
				Open Health
			</button>
			{#if showWarning}
				<button
					type="button"
					class="text-[11px] text-[#666] hover:text-white px-2 transition-colors"
					on:click={handleDismiss}
					aria-label="Dismiss"
				>
					✕
				</button>
			{/if}
		</div>
	</div>
{/if}
