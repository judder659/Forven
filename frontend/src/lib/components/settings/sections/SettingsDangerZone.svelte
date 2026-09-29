<script lang="ts">
	import { onMount } from 'svelte';
	import {
		getFactoryResetCategories,
		performFactoryReset,
		type FactoryResetCategory,
	} from '$lib/api/forven';

	// API-10: must equal `FACTORY_RESET_CONFIRM_PHRASE` in $lib/api/forven, which is
	// in turn the `Literal["FACTORY RESET"]` that FactoryResetBody accepts
	// (forven/routers/ops.py). Declared locally rather than imported so this
	// component stays mountable under a `vi.mock('$lib/api/forven')` factory;
	// hardenApiUi.test.ts drives the REAL client through this dialog and asserts the
	// posted body, so any drift between the three fails there rather than at runtime.
	const FACTORY_RESET_PHRASE = 'FACTORY RESET';

	// Accepted for parity with the other section components (the settings shell passes
	// it); the Danger Zone is a custom destructive-action panel and does not read the
	// flat settings blob.
	export let settings: Record<string, unknown> = {};
	void settings;

	let categories: FactoryResetCategory[] = [];
	let keep: Record<string, boolean> = {};
	let loading = true;
	let loadError: string | null = null;

	let confirmOpen = false;
	let confirmText = '';
	let resetting = false;
	let resultMessage: string | null = null;
	let resultError: string | null = null;

	onMount(async () => {
		try {
			const res = await getFactoryResetCategories();
			categories = res.categories ?? [];
			const next: Record<string, boolean> = {};
			for (const c of categories) next[c.id] = !!c.default_keep;
			keep = next;
		} catch (e) {
			loadError = e instanceof Error ? e.message : 'Failed to load reset categories.';
		} finally {
			loading = false;
		}
	});

	$: keepIds = categories.filter((c) => keep[c.id]).map((c) => c.id);
	$: wipeLabels = categories.filter((c) => !keep[c.id]).map((c) => c.label);
	// API-10: the phrase is now BOTH the local arming check and the `confirm_phrase`
	// the backend requires (a pydantic Literal). It used to be the word 'RESET',
	// checked client-side only and never sent — so the wipe was one un-typed POST
	// away for anything that could reach the API. Compared case-sensitively: the
	// backend Literal is exact, so accepting 'factory reset' here would arm the
	// button for a request the server then 422s.
	$: confirmArmed = confirmText.trim() === FACTORY_RESET_PHRASE;

	function toggleKeep(id: string, checked: boolean): void {
		keep = { ...keep, [id]: checked };
	}

	function openConfirm(): void {
		confirmText = '';
		resultError = null;
		resultMessage = null;
		confirmOpen = true;
	}

	function cancelConfirm(): void {
		confirmOpen = false;
	}

	async function doReset(): Promise<void> {
		if (!confirmArmed || resetting) return;
		resetting = true;
		resultError = null;
		resultMessage = null;
		try {
			const res = await performFactoryReset(keepIds);
			const wiped = (res.wiped ?? []).join(', ') || 'nothing';
			const kept = (res.kept ?? []).join(', ') || 'nothing';
			resultMessage = `Factory reset complete. Wiped: ${wiped}. Kept: ${kept}.`;
			confirmOpen = false;
		} catch (e) {
			resultError = e instanceof Error ? e.message : 'Factory reset failed.';
		} finally {
			resetting = false;
		}
	}
</script>

<div class="space-y-6">
	<div class="border border-red-900 bg-red-500/5 p-5 space-y-4">
		<div>
			<h2 class="text-sm font-bold uppercase tracking-widest text-red-400">Factory reset</h2>
			<p class="text-xs text-red-400/80 mt-1">
				Permanently wipes the selected data categories and restores a clean slate. This cannot be
				undone. Choose which categories to <strong>keep</strong> — everything else is erased.
			</p>
		</div>

		{#if loading}
			<p class="text-xs text-sc-ink3">Loading reset categories…</p>
		{:else if loadError}
			<p class="text-xs text-red-400">Could not load categories: {loadError}</p>
		{:else}
			<ul class="space-y-2">
				{#each categories as cat (cat.id)}
					<li class="flex items-start gap-3">
						<input
							id={`keep-${cat.id}`}
							type="checkbox"
							checked={keep[cat.id]}
							on:change={(e) => toggleKeep(cat.id, e.currentTarget.checked)}
							class="mt-1 accent-red-500"
						/>
						<label for={`keep-${cat.id}`} class="text-xs text-sc-ink2 leading-tight">
							<span class="font-bold text-sc-ink">Keep {cat.label}</span>
							{#if cat.description}<span class="block text-sc-ink3">{cat.description}</span>{/if}
						</label>
					</li>
				{/each}
			</ul>

			<p class="text-xs text-red-400/80">
				{#if wipeLabels.length}
					Will wipe: {wipeLabels.join(', ')}.
				{:else}
					Nothing selected to wipe — every category is kept.
				{/if}
			</p>

			<button
				type="button"
				on:click={openConfirm}
				disabled={categories.length === 0}
				class="terminal-button-danger text-[12px]"
			>
				Wipe &amp; factory reset…
			</button>
		{/if}

		{#if resultMessage}
			<p class="text-xs text-emerald-400">{resultMessage}</p>
		{/if}
		{#if resultError}
			<p class="text-xs text-red-400">{resultError}</p>
		{/if}
	</div>
</div>

{#if confirmOpen}
	<div
		class="fixed inset-0 z-50 flex items-center justify-center bg-sc-bg/80 p-4"
		role="dialog"
		aria-modal="true"
		aria-labelledby="factory-reset-title"
	>
		<div class="rounded-md w-full max-w-md border border-red-900 bg-sc-panel p-5 space-y-4">
			<h2 id="factory-reset-title" class="text-sm font-bold uppercase tracking-widest text-red-400">
				Confirm factory reset
			</h2>
			<p class="text-xs text-sc-ink2">
				This will permanently wipe:
				<strong class="text-red-400">{wipeLabels.join(', ') || 'nothing'}</strong>. This action
				cannot be undone.
			</p>
			<label class="block text-xs text-sc-ink3">
				Type <span class="text-red-400">{FACTORY_RESET_PHRASE}</span> to confirm:
				<input
					type="text"
					bind:value={confirmText}
					class="terminal-input mt-1 w-full"
					autocomplete="off"
				/>
			</label>
			<div class="flex justify-end gap-2">
				<button
					type="button"
					on:click={cancelConfirm}
					class="terminal-button text-[12px]"
				>
					Cancel
				</button>
				<button
					type="button"
					on:click={doReset}
					disabled={!confirmArmed || resetting}
					class="terminal-button-danger text-[12px]"
				>
					{resetting ? 'Resetting…' : 'Wipe everything'}
				</button>
			</div>
		</div>
	</div>
{/if}
