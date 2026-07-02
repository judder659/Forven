<script lang="ts">
	import { onMount } from 'svelte';

	// Assume acknowledged until storage is read so the banner doesn't flash on
	// every navigation for users who already dismissed it.
	let acknowledged = true;

	const STORAGE_KEY = 'forven.risk_disclaimer.ack';
	const DISCLAIMER_URL = 'https://github.com/judder659/Forven/blob/main/DISCLAIMER.md';

	function acknowledge() {
		acknowledged = true;
		try {
			localStorage.setItem(STORAGE_KEY, '1');
		} catch {
			// localStorage unavailable; dismiss for the lifetime of this mount only.
		}
	}

	onMount(() => {
		try {
			acknowledged = localStorage.getItem(STORAGE_KEY) === '1';
		} catch {
			acknowledged = false;
		}
	});
</script>

{#if !acknowledged}
	<div
		class="border-b border-yellow-900 bg-yellow-500/5 text-yellow-400 px-4 py-2 flex items-center justify-between gap-3"
		role="alert"
	>
		<div class="flex items-center gap-3 min-w-0">
			<svg class="w-4 h-4 text-yellow-400 shrink-0" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
				<path d="M12 2L1 21h22L12 2zm0 4.83L19.53 19H4.47L12 6.83zM11 10v4h2v-4h-2zm0 6v2h2v-2h-2z" />
			</svg>
			<div class="text-[11px] leading-snug min-w-0">
				<span class="font-bold">Paper + testnet only.</span>
				Backtest and paper-trading metrics are <span class="font-semibold">simulated</span>, may be
				inaccurate, and do not predict live results. Nothing here is financial advice — use entirely at
				your own risk.
				<a
					href={DISCLAIMER_URL}
					target="_blank"
					rel="noopener noreferrer"
					class="underline hover:text-yellow-300">Full disclaimer</a
				>.
			</div>
		</div>
		<button
			type="button"
			class="text-[11px] border border-yellow-900 text-yellow-400 px-2.5 py-1 hover:bg-yellow-500/10 transition-colors shrink-0"
			on:click={acknowledge}
		>
			Acknowledge
		</button>
	</div>
{/if}
