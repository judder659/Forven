<script lang="ts">
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { getForvenModelPolicy, getSettings } from '$lib/api/forven';

	// Free OpenRouter routes (":free" model suffix) have tight per-minute and
	// per-day caps; running the pipeline at balanced/max cadence against one
	// guarantees provider 429s. One-shot check on mount (model policy and the
	// effective preset both change rarely — a reload re-evaluates).
	let dismissed = false;
	let primaryModel = '';
	let isFreeRoute = false;
	let effectivePreset = '';

	const SESSION_KEY = 'forven.throughput_banner.dismissed';

	async function refresh() {
		try {
			const [policy, settings] = await Promise.all([getForvenModelPolicy(), getSettings()]);
			primaryModel = String(policy?.primary_model ?? '');
			isFreeRoute =
				String(policy?.primary_provider ?? '') === 'openrouter' &&
				primaryModel.toLowerCase().endsWith(':free');
			effectivePreset = String(settings?.throughput_preset_effective ?? '');
		} catch {
			// Best-effort hint; stay hidden on error.
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

	function openSettings() {
		goto('/settings');
	}

	onMount(() => {
		try {
			dismissed = sessionStorage.getItem(SESSION_KEY) === '1';
		} catch {
			dismissed = false;
		}
		void refresh();
	});

	$: visible =
		!dismissed && isFreeRoute && (effectivePreset === 'balanced' || effectivePreset === 'max');
</script>

{#if visible}
	<div
		class="border-b border-yellow-900 bg-yellow-500/5 text-yellow-400 px-4 py-2 flex items-center justify-between gap-3"
		role="status"
		data-testid="throughput-suggestion-banner"
	>
		<div class="flex items-center gap-3 min-w-0">
			<svg
				class="w-4 h-4 text-yellow-400 shrink-0"
				viewBox="0 0 24 24"
				fill="currentColor"
				aria-hidden="true"
			>
				<path d="M12 2L1 21h22L12 2zm0 4.83L19.53 19H4.47L12 6.83zM11 10v4h2v-4h-2zm0 6v2h2v-2h-2z" />
			</svg>
			<div class="text-[11px] leading-snug min-w-0">
				Your primary model is an OpenRouter free route
				(<span class="font-semibold">{primaryModel}</span>). The
				<span class="font-semibold">{effectivePreset}</span> throughput preset will likely hit its
				rate limits — consider the <span class="font-bold">Conserve</span> preset in Settings →
				System → Throughput.
			</div>
		</div>
		<div class="flex items-center gap-2 shrink-0">
			<button
				type="button"
				class="rounded-md text-[11px] border border-yellow-900 text-yellow-400 px-2.5 py-1 hover:bg-yellow-500/10 transition-colors"
				on:click={openSettings}
			>
				Open Settings
			</button>
			<button
				type="button"
				class="text-[11px] text-sc-ink3 hover:text-sc-ink px-2 transition-colors"
				on:click={handleDismiss}
				aria-label="Dismiss"
			>
				✕
			</button>
		</div>
	</div>
{/if}
