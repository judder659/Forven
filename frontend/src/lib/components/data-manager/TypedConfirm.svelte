<script lang="ts">
	// Inline confirmation for destructive actions: the exact phrase must be typed.
	import { createEventDispatcher, onMount } from 'svelte';

	export let phrase: string;
	export let action = 'Confirm';
	export let busy = false;
	export let danger = true;

	const dispatch = createEventDispatcher<{ confirm: void; cancel: void }>();
	let typed = '';
	let input: HTMLInputElement | undefined;
	onMount(() => input?.focus());
	$: ok = typed.trim() === phrase;

	function submit() {
		if (ok && !busy) dispatch('confirm');
	}
	function onKey(event: KeyboardEvent) {
		if (event.key === 'Escape') {
			event.stopPropagation();
			dispatch('cancel');
		}
	}
</script>

<form on:submit|preventDefault={submit} on:keydown={onKey} class="space-y-2">
	<slot />
	<label class="block text-[11px] text-[#888]">
		Type <span class="select-all font-mono text-white">{phrase}</span> to confirm
		<input bind:this={input} bind:value={typed} autocomplete="off" spellcheck="false" placeholder={phrase}
			class="terminal-input mt-1 font-mono text-[12px]" aria-label={`Type ${phrase} to confirm`} />
	</label>
	<div class="flex justify-end gap-2">
		<button type="button" class="terminal-button text-[10px]" on:click={() => dispatch('cancel')}>Cancel</button>
		<button type="submit" disabled={!ok || busy}
			class="{danger ? 'terminal-button-danger' : 'terminal-button-primary'} text-[10px] disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:bg-transparent disabled:hover:text-red-500">
			{busy ? 'Working…' : action}
		</button>
	</div>
</form>
