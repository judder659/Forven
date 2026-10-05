<script lang="ts">
	/**
	 * Reason picker for denying a strategy dethrone/promotion. Denying one arms
	 * an escalating cooldown on the backend, so it carries a real reason. Used
	 * by both the approval card and the inspector drawer.
	 */
	import { createEventDispatcher } from 'svelte';

	export let busy = false;

	const DENY_PRESETS = ['Performing fine', 'Insufficient evidence', 'Wrong target stage'];
	const dispatch = createEventDispatcher<{ confirm: { reason: string }; cancel: void }>();

	let preset = '';
	let freeText = '';

	function confirm() {
		const reason = [preset, freeText.trim()].filter(Boolean).join(' — ') || 'Denied via UI';
		dispatch('confirm', { reason });
	}
</script>

<div class="border border-red-900/60 bg-red-950/15 p-3 space-y-2" data-testid="deny-reason-picker">
	<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-red-300">Why deny?</div>
	<div class="flex flex-wrap gap-2">
		{#each DENY_PRESETS as option}
			<button
				type="button"
				class="rounded-md text-[12px] border px-3 py-1.5 {preset === option ? 'border-red-500 bg-red-900/40 text-red-200' : 'border-sc-line2 text-sc-ink2 hover:text-sc-ink'}"
				on:click={() => (preset = preset === option ? '' : option)}
			>
				{option}
			</button>
		{/each}
	</div>
	<input type="text" placeholder="Optional details..." class="rounded-md w-full bg-sc-bg border border-sc-line text-xs px-3 py-2 text-sc-ink" bind:value={freeText} />
	<div class="text-[10px] text-sc-ink3">Denying pauses dethrone re-asks for this strategy: 24h on the first deny, then 3 days, then 7 days. Approving one (or the strategy leaving paper/live) resets the ladder.</div>
	<div class="flex gap-2">
		<button type="button" disabled={busy} class="terminal-button-danger text-[12px] px-3 py-2 disabled:opacity-40" on:click={confirm}>Confirm deny</button>
		<button type="button" class="terminal-button text-[12px] px-3 py-2" on:click={() => dispatch('cancel')}>Cancel</button>
	</div>
</div>
