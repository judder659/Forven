<script lang="ts">
	import { onMount } from 'svelte';
	import { fade, fly } from 'svelte/transition';
	import {
		readJustUpdated,
		clearJustUpdated,
		updatePanelOpen,
		type JustUpdated,
	} from '$lib/stores/updateStatus';

	// Shown once, on the first page load after an update restarted Forven.
	let record: JustUpdated | null = null;

	onMount(() => {
		record = readJustUpdated();
	});

	function close() {
		clearJustUpdated();
		record = null;
	}

	function seeAll() {
		close();
		updatePanelOpen.set(true);
	}
</script>

{#if record}
	<div class="fixed inset-0 z-[1002] flex items-center justify-center bg-black/50 p-4" transition:fade={{ duration: 150 }}>
		<div
			class="w-full max-w-sm rounded-lg border border-sc-line2 bg-sc-panel p-5 shadow-2xl"
			transition:fly={{ y: 16, duration: 200 }}
			role="dialog"
			aria-modal="true"
			aria-labelledby="after-update-title"
			data-testid="after-update-card"
		>
			<div class="mb-3 flex h-9 w-9 items-center justify-center rounded-full bg-emerald-500/15 text-emerald-400">
				<svg class="h-5 w-5" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
					<path fill-rule="evenodd" d="M16.7 5.3a1 1 0 0 1 0 1.4l-8 8a1 1 0 0 1-1.4 0l-4-4a1 1 0 1 1 1.4-1.4L8 12.58l7.3-7.3a1 1 0 0 1 1.4 0Z" clip-rule="evenodd" />
				</svg>
			</div>
			<h2 id="after-update-title" class="text-[17px] font-semibold text-sc-ink">
				{record.to_version ? `You're on Forven ${record.to_version}` : "You're on the newest Forven"}
			</h2>
			{#if record.from_version}
				<p class="mt-0.5 text-[12px] text-sc-ink3">Updated from {record.from_version}</p>
			{/if}
			{#if record.highlights.length > 0}
				<ul class="mt-4 space-y-1.5">
					{#each record.highlights as item, i (i)}
						<li class="flex gap-2 text-[13px] leading-snug text-sc-ink2">
							<span class="mt-[7px] h-1 w-1 shrink-0 rounded-full bg-emerald-400" aria-hidden="true"></span>
							<span>{item}</span>
						</li>
					{/each}
				</ul>
			{/if}
			<div class="mt-5 flex justify-end gap-2">
				<button type="button" class="terminal-button" on:click={seeAll}>See all changes</button>
				<button type="button" class="terminal-button-primary" on:click={close}>Got it</button>
			</div>
		</div>
	</div>
{/if}
