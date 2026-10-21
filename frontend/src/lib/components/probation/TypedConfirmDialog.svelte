<script lang="ts">
	/**
	 * A confirm dialog that needs a typed phrase (PROBATION, GO LIVE). Optional
	 * bypass list: failing checks the operator may override, shown in red so the
	 * override is never silent.
	 */
	import { createEventDispatcher, onMount, tick } from 'svelte';
	import type { ProbationCheck } from '$lib/api/probation';

	export let title: string;
	export let phrase: string;
	export let message = '';
	export let confirmLabel = 'Confirm';
	export let busy = false;
	export let errorMessage: string | null = null;
	/** Failing checks the confirm will bypass; empty when nothing is bypassed. */
	export let bypassing: ProbationCheck[] = [];
	export let notes: string[] = [];
	export let askReason = false;
	export let danger = false;

	const dispatch = createEventDispatcher<{ cancel: void; confirm: { phrase: string; reason: string } }>();

	let typed = '';
	let reason = '';
	let input: HTMLInputElement;

	$: phraseOk = typed.trim().toUpperCase() === phrase.toUpperCase();

	onMount(async () => {
		await tick();
		input?.focus();
	});

	function onKey(event: KeyboardEvent) {
		if (event.key === 'Escape' && !busy) dispatch('cancel');
	}

	function confirm() {
		if (busy || !phraseOk) return;
		dispatch('confirm', { phrase: typed.trim().toUpperCase(), reason: reason.trim() });
	}
</script>

<svelte:window on:keydown={onKey} />

<div
	class="fixed inset-0 z-[60] grid place-items-center bg-[#040507]/70 p-4"
	role="presentation"
	on:click={(event) => { if (event.target === event.currentTarget && !busy) dispatch('cancel'); }}
>
	<div
		class="grid w-full max-w-md gap-3 rounded-lg border bg-sc-panel p-4 shadow-2xl {danger ? 'border-[#e5574f]/50' : 'border-amber-400/50'}"
		role="dialog"
		aria-modal="true"
		aria-labelledby="typed-confirm-title"
		data-testid="typed-confirm-dialog"
	>
		<h2 id="typed-confirm-title" class="text-[15px] font-semibold text-sc-ink">{title}</h2>
		{#if message}
			<div class="rounded border px-2.5 py-2 text-[12px] {danger ? 'border-[#e5574f]/45 bg-[#e5574f]/10 text-[#f3b1ab]' : 'border-amber-400/40 bg-amber-400/10 text-amber-100'}">{message}</div>
		{/if}
		{#if bypassing.length}
			<div class="grid gap-1 rounded border border-red-800 bg-red-900/20 px-2.5 py-2 text-[12px] text-red-200" data-testid="typed-confirm-bypass">
				<span class="font-semibold">You are overriding {bypassing.length === 1 ? 'a check that failed' : `${bypassing.length} checks that failed`}:</span>
				{#each bypassing as check (check.key)}
					<span>• {check.label}: {check.detail}</span>
				{/each}
			</div>
		{/if}
		{#each notes as note}
			<div class="text-[12px] text-sc-ink2">{note}</div>
		{/each}
		{#if askReason}
			<label class="grid gap-1 text-[12px] text-sc-ink3">
				<span>Reason (optional, kept in the history)</span>
				<input type="text" bind:value={reason} class="terminal-input" data-testid="typed-confirm-reason" />
			</label>
		{/if}
		<label class="grid gap-1 text-[12px] text-sc-ink3">
			<span>Type <span class="font-semibold {danger ? 'text-red-400' : 'text-amber-300'}">{phrase}</span> to confirm</span>
			<input
				bind:this={input}
				type="text"
				bind:value={typed}
				placeholder={phrase}
				class="terminal-input"
				data-testid="typed-confirm-phrase"
				on:keydown={(event) => { if (event.key === 'Enter') confirm(); }}
			/>
		</label>
		{#if errorMessage}
			<div class="rounded border border-red-800 bg-red-900/20 px-2.5 py-2 text-[12px] text-red-300" data-testid="typed-confirm-error">{errorMessage}</div>
		{/if}
		<div class="flex flex-wrap justify-end gap-2">
			<button type="button" class="rounded border border-sc-line px-3 py-1.5 text-[12px] text-sc-ink2 hover:bg-sc-raise" disabled={busy} on:click={() => dispatch('cancel')}>Cancel</button>
			<button
				type="button"
				class="rounded border px-3 py-1.5 text-[12px] font-semibold disabled:opacity-40 {danger ? 'border-red-700 bg-red-900/40 text-red-100 hover:bg-red-900/60' : 'border-amber-500/60 bg-amber-500/20 text-amber-100 hover:bg-amber-500/30'}"
				disabled={busy || !phraseOk}
				data-testid="typed-confirm-submit"
				on:click={confirm}
			>{busy ? 'Working…' : confirmLabel}</button>
		</div>
	</div>
</div>
