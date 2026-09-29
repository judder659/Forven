<script lang="ts">
	// Loading, not-deployed-yet and error states for one section of the page,
	// so a missing backend route never blanks the view. The slot renders once
	// data is there; a failed refresh keeps it and notes the error.
	import { createEventDispatcher } from 'svelte';
	import Skeleton from '$lib/components/Skeleton.svelte';
	import type { Loadable } from '$lib/stores/dataManager';

	export let state: Loadable<unknown>;
	/** What the section shows, e.g. "The freshness census". */
	export let what: string;
	/** The route it reads, e.g. "GET /api/data/sla". */
	export let endpoint = '';
	export let rows = 3;

	const dispatch = createEventDispatcher<{ retry: void }>();
</script>

{#if state.status === 'loading'}
	<div class="p-3" aria-busy="true" aria-label="Loading">
		<Skeleton {rows} />
	</div>
{:else if state.status === 'unavailable'}
	<div class="m-3 border border-dashed border-sc-line2 px-3 py-2.5 text-[12px] text-sc-ink2" role="status">
		<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Not available yet</div>
		<p class="mt-1 leading-relaxed">
			{what} needs {#if endpoint}<span class="font-mono text-[11px] text-sc-ink2">{endpoint}</span>{:else}a backend route{/if}, which this
			backend does not serve yet. It arrives with the Data Manager backend update.
		</p>
		<button type="button" on:click={() => dispatch('retry')}
			class="rounded-md mt-2 border border-sc-line2 px-2 py-0.5 text-[12px] text-sc-ink2 hover:border-sc-ink hover:text-sc-ink">Check again</button>
	</div>
{:else if state.status === 'error'}
	<div class="m-3 flex flex-wrap items-center gap-2 border border-red-900 bg-red-500/5 px-3 py-2 text-[12px] text-red-400" role="alert">
		<span class="min-w-0 flex-1">Could not load {what.charAt(0).toLowerCase() + what.slice(1)}: {state.error}</span>
		<button type="button" on:click={() => dispatch('retry')}
			class="rounded-md border border-red-900 px-2 py-0.5 text-[12px] text-red-300 hover:border-red-400 hover:text-sc-ink">Retry</button>
	</div>
{:else}
	<slot />
	{#if state.error}
		<div class="px-3 pb-2 text-[10px] text-amber-500/80" role="status">Refresh failed ({state.error}); showing the last data.</div>
	{/if}
{/if}
