<script lang="ts" context="module">
	export interface ConfirmSpec {
		title: string;
		/** Real-money or side-effect warning shown above the rows. */
		warn?: string;
		rows: Array<[string, string]>;
		cta: string;
		/** Styles the confirm button as destructive (closes, flips, live opens). */
		danger?: boolean;
		run: () => Promise<void>;
	}
</script>

<script lang="ts">
	import { createEventDispatcher, onMount, tick } from 'svelte';

	export let spec: ConfirmSpec;
	export let busy = false;
	export let live = false;

	const dispatch = createEventDispatcher<{ cancel: void; confirm: void }>();
	let cancelButton: HTMLButtonElement;

	onMount(async () => {
		await tick();
		cancelButton?.focus();
	});

	function onKey(event: KeyboardEvent) {
		if (event.key === 'Escape' && !busy) dispatch('cancel');
	}
</script>

<svelte:window on:keydown={onKey} />

<div
	class="fixed inset-0 z-50 grid place-items-center bg-[#040507]/70 p-4"
	role="presentation"
	on:click={(event) => { if (event.target === event.currentTarget && !busy) dispatch('cancel'); }}
>
	<div class="grid w-full max-w-md gap-3 rounded-lg border border-sc-line2 bg-[#0b0d11] p-4 shadow-2xl" role="dialog" aria-modal="true" aria-labelledby="desk-confirm-title" data-testid="desk-confirm">
		<h2 id="desk-confirm-title" class="text-[15px] font-semibold text-sc-ink">{spec.title}</h2>
		{#if spec.warn}
			<div class={`rounded border px-2.5 py-2 text-[12px] ${live ? 'border-[#e5574f]/45 bg-[#e5574f]/10 text-[#f3b1ab]' : 'border-sc-line2 bg-sc-panel2 text-sc-ink2'}`}>{spec.warn}</div>
		{/if}
		<div class="grid gap-1">
			{#each spec.rows as [key, value]}
				<div class="grid grid-cols-[minmax(0,1fr)_auto] gap-3 text-[12px]">
					<span class="text-sc-ink3">{key}</span>
					<span class="text-right font-plex-mono text-sc-ink">{value}</span>
				</div>
			{/each}
		</div>
		<div class="flex flex-wrap justify-end gap-2">
			<button bind:this={cancelButton} type="button" class="rounded-md border border-sc-line2 bg-sc-panel2 px-3 py-1.5 text-[12.5px] font-medium text-sc-ink hover:border-sc-ink4 disabled:opacity-50" disabled={busy} on:click={() => dispatch('cancel')}>Cancel</button>
			<button
				type="button"
				class={`rounded-md border px-3 py-1.5 text-[12.5px] font-medium disabled:opacity-50 ${spec.danger ? 'border-[#e5574f]/60 bg-[#e5574f]/15 text-[#f6b4ae] hover:bg-[#e5574f]/25' : 'border-sc-ink4 bg-sc-raise text-sc-ink hover:border-sc-ink3'}`}
				disabled={busy}
				on:click={() => dispatch('confirm')}
				data-testid="desk-confirm-go"
			>{busy ? 'Working…' : spec.cta}</button>
		</div>
	</div>
</div>
