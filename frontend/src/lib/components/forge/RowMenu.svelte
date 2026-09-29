<script lang="ts" context="module">
	/** Stage moves the Forge offers; values are the aliases the transition API accepts. */
	export const MOVE_TARGETS: Array<{ value: string; label: string; stage: string }> = [
		{ value: 'researching', label: 'Quick screen', stage: 'quick_screen' },
		{ value: 'backtesting', label: 'Gauntlet', stage: 'gauntlet' },
		{ value: 'paper_trading', label: 'Paper', stage: 'paper' },
		{ value: 'deployed', label: 'Live', stage: 'live_graduated' },
		{ value: 'rejected', label: 'Rejected', stage: 'rejected' },
	];
</script>

<script lang="ts">
	import { createEventDispatcher, onDestroy } from 'svelte';

	export let bucket: 'active' | 'trash' = 'active';
	/** Current stage, so the menu doesn't offer a move to where it already is. */
	export let stage = '';
	export let label = 'Strategy actions';

	const dispatch = createEventDispatcher<{ open: void; move: string; graveyard: void; delete: void; recover: void; export: 'download' | 'clipboard' }>();

	let open = false;
	let root: HTMLDivElement;

	function toggle(event: MouseEvent) {
		event.stopPropagation();
		open = !open;
	}

	function fire(action: 'open' | 'graveyard' | 'delete' | 'recover') {
		open = false;
		dispatch(action);
	}

	function move(value: string) {
		open = false;
		dispatch('move', value);
	}

	function exportAs(mode: 'download' | 'clipboard') {
		open = false;
		dispatch('export', mode);
	}

	function onWindowClick(event: MouseEvent) {
		if (open && root && !root.contains(event.target as Node)) open = false;
	}

	function onKey(event: KeyboardEvent) {
		if (open && event.key === 'Escape') open = false;
	}

	onDestroy(() => {
		open = false;
	});
</script>

<svelte:window on:click={onWindowClick} on:keydown={onKey} />

<div class="relative inline-block text-left" bind:this={root}>
	<button
		type="button"
		class="grid h-6 w-7 place-items-center rounded text-[15px] leading-none text-sc-ink3 transition-colors hover:bg-sc-raise hover:text-sc-ink {open ? 'bg-sc-raise text-sc-ink' : ''}"
		aria-haspopup="menu"
		aria-expanded={open}
		aria-label={label}
		on:click={toggle}
	>⋯</button>
	{#if open}
		<div
			role="menu"
			class="absolute right-0 top-full z-40 mt-1 w-52 overflow-hidden rounded-md border border-sc-line2 bg-sc-panel2 py-1 text-[12px] shadow-[0_12px_32px_rgba(0,0,0,0.5)]"
		>
			<button type="button" role="menuitem" class="block w-full px-3 py-1.5 text-left text-sc-ink hover:bg-sc-raise" on:click|stopPropagation={() => fire('open')}>Open strategy page</button>
			<button type="button" role="menuitem" class="block w-full px-3 py-1.5 text-left text-sc-ink2 hover:bg-sc-raise hover:text-sc-ink" on:click|stopPropagation={() => exportAs('download')}>Export to .json</button>
			<button type="button" role="menuitem" class="block w-full px-3 py-1.5 text-left text-sc-ink2 hover:bg-sc-raise hover:text-sc-ink" on:click|stopPropagation={() => exportAs('clipboard')}>Copy export to clipboard</button>
			{#if bucket === 'active'}
				<div class="mt-1 border-t border-sc-line px-3 pb-0.5 pt-1.5 font-plex-cond text-[10.5px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Move to</div>
				{#each MOVE_TARGETS.filter((t) => t.stage !== stage) as target (target.value)}
					<button type="button" role="menuitem" class="block w-full px-3 py-1.5 text-left text-sc-ink2 hover:bg-sc-raise hover:text-sc-ink" on:click|stopPropagation={() => move(target.value)}>{target.label}</button>
				{/each}
				<div class="mt-1 border-t border-sc-line pt-1"></div>
				<button type="button" role="menuitem" class="block w-full px-3 py-1.5 text-left text-[#e7b24a] hover:bg-sc-raise" on:click|stopPropagation={() => fire('graveyard')}>Send to graveyard</button>
			{:else}
				<button type="button" role="menuitem" class="block w-full px-3 py-1.5 text-left text-[#3cc48f] hover:bg-sc-raise" on:click|stopPropagation={() => fire('recover')}>Recover from graveyard</button>
			{/if}
			<button type="button" role="menuitem" class="block w-full px-3 py-1.5 text-left text-[#f2956f] hover:bg-sc-raise" on:click|stopPropagation={() => fire('delete')}>Delete permanently</button>
		</div>
	{/if}
</div>
