<script lang="ts">
	import { createEventDispatcher } from 'svelte';
	import type { Writable } from 'svelte/store';
	import type { AgentHubSettings } from './agentHubSettings';

	import { fade, fly } from 'svelte/transition';

	export let settings: Writable<AgentHubSettings>;

	const dispatch = createEventDispatcher<{ close: void; reset: void }>();

	const pollIntervalOptions = [3000, 5000, 10000, 15000, 30000];

	const defaultSettings: AgentHubSettings = {
		pollInterval: 5000,
		compactCards: false,
		soundOnComplete: false,
		showInternalWorkers: true,
		showSchedulerErrors: false
	};

	$: current = $settings;

	function closeDrawer() {
		dispatch('close');
	}

	function resetSettings() {
		settings.set({ ...defaultSettings });
		dispatch('close');
		dispatch('reset');
	}

	function formatPollInterval(ms: number): string {
		return `${ms / 1000}s`;
	}
</script>

<div class="fixed inset-0 z-50">
	<button
		type="button"
		aria-label="Close settings drawer"
		class="absolute inset-0 bg-black/80"
		transition:fade
		on:click={closeDrawer}
	></button>
	<div
		class="absolute right-0 top-0 h-full w-full max-w-sm bg-[#050505] border-l border-[#222] flex flex-col text-sm text-[#aaa]"
		transition:fly={{ x: 320 }}
	>
		<div class="px-4 py-3 border-b border-[#222] flex items-center justify-between sticky top-0 bg-[#0a0a0a]">
			<h2 class="font-bold tracking-wider uppercase text-xs">Agent Hub Settings</h2>
			<button class="terminal-button-icon" type="button" on:click={closeDrawer} aria-label="Close settings">
				<svg class="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
					<line x1="18" y1="6" x2="6" y2="18"></line>
					<line x1="6" y1="6" x2="18" y2="18"></line>
				</svg>
			</button>
		</div>
		<div class="p-4 space-y-6 overflow-y-auto flex-1 min-h-0">
			<section class="space-y-2">
				<h3 class="text-[11px] uppercase tracking-wider text-gray-500 mb-2">Polling</h3>
				<div>
					<label class="block text-[10px] text-gray-500 uppercase tracking-wider mb-1" for="hub-poll-interval">
						Poll Interval
					</label>
					<select
						id="hub-poll-interval"
						class="terminal-select"
						bind:value={current.pollInterval}
					>
						{#each pollIntervalOptions as option}
							<option value={option}>{formatPollInterval(option)}</option>
						{/each}
					</select>
				</div>
			</section>

			<section class="space-y-4">
				<h3 class="text-[11px] uppercase tracking-wider text-gray-500 mb-2">Display</h3>
				<label class="flex items-center justify-between text-xs cursor-pointer">
					<span class="uppercase tracking-wider text-[10px] text-gray-500">Compact Card Mode</span>
					<input
						type="checkbox"
						checked={current.compactCards}
						on:change={(event) => settings.update((value) => ({ ...value, compactCards: (event.currentTarget as HTMLInputElement).checked }))}
					/>
				</label>
				<label class="flex items-center justify-between text-xs cursor-pointer">
					<span class="uppercase tracking-wider text-[10px] text-gray-500">Sound on Task Completion</span>
					<input
						type="checkbox"
						checked={current.soundOnComplete}
						on:change={(event) => settings.update((value) => ({ ...value, soundOnComplete: (event.currentTarget as HTMLInputElement).checked }))}
					/>
				</label>
				<label class="flex items-center justify-between text-xs cursor-pointer">
					<span class="uppercase tracking-wider text-[10px] text-gray-500">Show Internal Workers</span>
					<input
						type="checkbox"
						checked={current.showInternalWorkers}
						on:change={(event) => settings.update((value) => ({ ...value, showInternalWorkers: (event.currentTarget as HTMLInputElement).checked }))}
					/>
				</label>
			</section>

			<section class="space-y-3">
				<h3 class="text-[11px] uppercase tracking-wider text-gray-500 mb-2">Scheduler</h3>
				<label class="flex items-center justify-between text-xs cursor-pointer">
					<span class="uppercase tracking-wider text-[10px] text-gray-500">Auto-expand errors</span>
					<input
						type="checkbox"
						checked={current.showSchedulerErrors}
						on:change={(event) => settings.update((value) => ({ ...value, showSchedulerErrors: (event.currentTarget as HTMLInputElement).checked }))}
					/>
				</label>
			</section>

			<section class="pt-2">
				<button type="button" class="w-full terminal-button-danger" on:click={resetSettings}>Reset to Defaults</button>
			</section>
		</div>
	</div>
</div>
