<script lang="ts" context="module">
	export interface ConfirmDialogSpec {
		title: string;
		/** Real-money or side-effect warning shown above the rows. */
		warn?: string;
		rows?: Array<[string, string]>;
		cta: string;
		/** Styles the confirm button as destructive (real money, deletes). */
		danger?: boolean;
		/** When set, the confirm button stays disabled until this phrase is typed. */
		phrase?: string;
	}
</script>

<script lang="ts">
	import { createEventDispatcher, onMount, tick } from 'svelte';

	export let spec: ConfirmDialogSpec;
	export let busy = false;

	const dispatch = createEventDispatcher<{ cancel: void; confirm: void }>();
	let cancelButton: HTMLButtonElement;
	let typed = '';

	$: phraseOk = !spec.phrase || typed.trim().toUpperCase() === spec.phrase.toUpperCase();

	onMount(async () => {
		await tick();
		cancelButton?.focus();
	});

	function onKey(event: KeyboardEvent) {
		if (event.key === 'Escape' && !busy) dispatch('cancel');
	}

	function confirm() {
		if (busy || !phraseOk) return;
		dispatch('confirm');
	}
</script>

<svelte:window on:keydown={onKey} />

<div
	class="fixed inset-0 z-50 grid place-items-center bg-[#040507]/70 p-4"
	role="presentation"
	on:click={(event) => { if (event.target === event.currentTarget && !busy) dispatch('cancel'); }}
>
	<div class="grid w-full max-w-md gap-3 rounded-lg border border-sc-line2 bg-sc-panel p-4 shadow-2xl" role="dialog" aria-modal="true" aria-labelledby="confirm-dialog-title" data-testid="confirm-dialog">
		<h2 id="confirm-dialog-title" class="text-[15px] font-semibold text-sc-ink">{spec.title}</h2>
		{#if spec.warn}
			<div class={`rounded border px-2.5 py-2 text-[12px] ${spec.danger ? 'border-[#e5574f]/45 bg-[#e5574f]/10 text-[#f3b1ab]' : 'border-sc-line2 bg-sc-panel2 text-sc-ink2'}`}>{spec.warn}</div>
		{/if}
		{#if spec.rows && spec.rows.length > 0}
			<div class="grid gap-1" data-testid="confirm-dialog-rows">
				{#each spec.rows as [key, value]}
					<div class="grid grid-cols-[minmax(0,1fr)_auto] gap-3 text-[12px]">
						<span class="text-sc-ink3">{key}</span>
						<span class="text-right font-plex-mono text-sc-ink">{value}</span>
					</div>
				{/each}
			</div>
		{/if}
		<slot />
		{#if spec.phrase}
			<label class="grid gap-1 text-[12px] text-sc-ink3">
				<span>Type <span class="font-semibold text-red-400">{spec.phrase}</span> to confirm</span>
				<input
					type="text"
					bind:value={typed}
					placeholder={spec.phrase}
					class="terminal-input"
					data-testid="confirm-dialog-phrase"
					on:keydown={(event) => { if (event.key === 'Enter') confirm(); }}
				/>
			</label>
		{/if}
		<div class="flex flex-wrap justify-end gap-2">
			<button bind:this={cancelButton} type="button" class="rounded-md border border-sc-line2 bg-sc-panel2 px-3 py-1.5 text-[12.5px] font-medium text-sc-ink hover:border-sc-ink4 disabled:opacity-50" disabled={busy} on:click={() => dispatch('cancel')}>Cancel</button>
			<button
				type="button"
				class={`rounded-md border px-3 py-1.5 text-[12.5px] font-medium disabled:opacity-50 ${spec.danger ? 'border-[#e5574f]/60 bg-[#e5574f]/15 text-[#f6b4ae] hover:bg-[#e5574f]/25' : 'border-sc-ink4 bg-sc-raise text-sc-ink hover:border-sc-ink3'}`}
				disabled={busy || !phraseOk}
				on:click={confirm}
				data-testid="confirm-dialog-go"
			>{busy ? 'Working…' : spec.cta}</button>
		</div>
	</div>
</div>
