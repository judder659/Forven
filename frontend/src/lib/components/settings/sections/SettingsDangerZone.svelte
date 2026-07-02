<script lang="ts">
	import { onMount } from 'svelte';
	import {
		getFactoryResetCategories,
		performFactoryReset,
		type FactoryResetCategory,
	} from '$lib/api/forven';

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
	$: confirmArmed = confirmText.trim().toUpperCase() === 'RESET';

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
			<p class="text-xs text-[#666]">Loading reset categories…</p>
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
						<label for={`keep-${cat.id}`} class="text-xs text-[#888] leading-tight">
							<span class="font-bold text-white">Keep {cat.label}</span>
							{#if cat.description}<span class="block text-[#666]">{cat.description}</span>{/if}
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
				class="terminal-button-danger text-xs"
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
		class="fixed inset-0 z-50 flex items-center justify-center bg-black/80 p-4"
		role="dialog"
		aria-modal="true"
		aria-labelledby="factory-reset-title"
	>
		<div class="w-full max-w-md border border-red-900 bg-[#050505] p-5 space-y-4">
			<h2 id="factory-reset-title" class="text-sm font-bold uppercase tracking-widest text-red-400">
				Confirm factory reset
			</h2>
			<p class="text-xs text-[#888]">
				This will permanently wipe:
				<strong class="text-red-400">{wipeLabels.join(', ') || 'nothing'}</strong>. This action
				cannot be undone.
			</p>
			<label class="block text-xs text-[#666]">
				Type <span class="text-red-400">RESET</span> to confirm:
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
					class="terminal-button text-xs"
				>
					Cancel
				</button>
				<button
					type="button"
					on:click={doReset}
					disabled={!confirmArmed || resetting}
					class="terminal-button-danger text-xs"
				>
					{resetting ? 'Resetting…' : 'Wipe everything'}
				</button>
			</div>
		</div>
	</div>
{/if}
