<script lang="ts">
	// One side of a condition, shown as a chip; its picker chooses a data
	// series, a knob (named parameter) or a fixed number.
	import { createEventDispatcher, tick } from 'svelte';
	import { anchored } from '$lib/actions/anchored';
	import { dismissable } from '$lib/actions/dismissable';

	type OperandType = 'series' | 'param' | 'const';
	export let type: OperandType;
	export let value: string | number;
	export let label: string;
	export let seriesGroups: Array<{ label: string; items: Array<{ value: string; label: string }> }> = [];
	export let knobs: Array<{ name: string; value: number }> = [];
	export let missing = false;
	export let disabled = false;
	export let ariaLabel = 'operand';

	const dispatch = createEventDispatcher<{
		change: { type: OperandType; value: string | number };
		addIndicator: void;
		newKnob: number;
	}>();

	let open = false;
	let tab: OperandType = type;
	let search = '';
	let draft = '';
	let searchInput: HTMLInputElement | undefined;
	let numberInput: HTMLInputElement | undefined;
	let chip: HTMLButtonElement | undefined;

	async function toggle() {
		if (disabled) return;
		open = !open;
		if (!open) return;
		tab = type;
		search = '';
		draft = type === 'const' ? String(value) : '';
		await tick();
		(tab === 'const' ? numberInput : searchInput)?.focus();
	}
	function pick(next: { type: OperandType; value: string | number }) {
		dispatch('change', next);
		open = false;
	}
	function applyNumber() {
		const n = Number(draft);
		if (draft.trim() !== '' && Number.isFinite(n)) pick({ type: 'const', value: n });
	}

	$: query = search.trim().toLowerCase();
	$: filtered = seriesGroups
		.map((group) => ({
			...group,
			items: group.items.filter((item) => !query || item.label.toLowerCase().includes(query) || item.value.toLowerCase().includes(query)),
		}))
		.filter((group) => group.items.length);
</script>

<span class="relative inline-block">
	<button type="button" bind:this={chip} on:click={toggle} {disabled} aria-label={ariaLabel} aria-haspopup="dialog" aria-expanded={open}
		class="rounded-md max-w-[220px] truncate border px-1.5 py-0.5 text-left text-[12px] transition-colors disabled:opacity-40
			{missing ? 'border-red-700 text-red-300' : type === 'const' ? 'border-sc-line2 font-mono text-sc-ink hover:border-sc-line2' : type === 'param' ? 'border-sky-900 text-sky-300 hover:border-sky-600' : 'border-sc-line2 text-sc-ink hover:border-sc-ink'}">
		{label}
	</button>
	{#if open}
		<div use:anchored={chip} use:dismissable={() => (open = false)} role="dialog" aria-label="choose a value"
			class="rounded-md fixed z-50 w-72 border border-sc-line2 bg-sc-panel shadow-2xl shadow-black">
			<div class="flex border-b border-sc-line font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em]">
				{#each [['series', 'Series'], ['param', 'Knob'], ['const', 'Number']] as [key, name]}
					<button type="button" on:click={async () => { tab = key as OperandType; await tick(); (tab === 'const' ? numberInput : searchInput)?.focus(); }}
						class="flex-1 px-2 py-1.5 {tab === key ? 'bg-sc-ink text-black' : 'text-sc-ink3 hover:text-sc-ink'}">{name}</button>
				{/each}
			</div>
			{#if tab === 'series'}
				<div class="p-2">
					<input bind:this={searchInput} bind:value={search} placeholder="Search series…" aria-label="search series"
						class="rounded-md w-full border border-sc-line2 bg-sc-bg px-2 py-1 text-[12px] text-sc-ink outline-none focus:border-sc-ink" />
				</div>
				<div class="max-h-60 overflow-y-auto pb-1">
					{#each filtered as group (group.label)}
						<div class="px-2 pt-1.5 font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">{group.label}</div>
						{#each group.items as item (item.value)}
							<button type="button" on:click={() => pick({ type: 'series', value: item.value })}
								class="flex w-full items-baseline justify-between gap-2 px-2 py-1 text-left text-[12px] hover:bg-sc-raise {type === 'series' && value === item.value ? 'text-sc-ink' : 'text-sc-ink2'}">
								<span class="truncate">{item.label}</span>
								<span class="shrink-0 font-mono text-[10px] text-sc-ink3">{item.value}</span>
							</button>
						{/each}
					{/each}
					{#if !filtered.length}<div class="px-2 py-2 text-[11px] text-sc-ink3">No series match “{search}”.</div>{/if}
				</div>
				<button type="button" on:click={() => { open = false; dispatch('addIndicator'); }}
					class="w-full border-t border-sc-line px-2 py-1.5 text-left text-[11px] text-sc-ink2 hover:text-sc-ink">＋ Add an indicator…</button>
			{:else if tab === 'param'}
				<div class="max-h-60 overflow-y-auto py-1">
					{#each knobs as knob (knob.name)}
						<button type="button" on:click={() => pick({ type: 'param', value: knob.name })}
							class="flex w-full items-baseline justify-between px-2 py-1 text-left text-[12px] hover:bg-sc-raise {type === 'param' && value === knob.name ? 'text-sky-300' : 'text-sc-ink2'}">
							<span>{knob.name}</span><span class="font-mono text-[11px] text-sc-ink3">{knob.value}</span>
						</button>
					{/each}
					{#if !knobs.length}<div class="px-2 py-2 text-[11px] text-sc-ink3">No knobs yet. Knobs are named numbers you can tune and stress-test.</div>{/if}
				</div>
				<button type="button" on:click={() => { open = false; dispatch('newKnob', type === 'const' ? Number(value) || 0 : 0); }}
					class="w-full border-t border-sc-line px-2 py-1.5 text-left text-[11px] text-sc-ink2 hover:text-sc-ink">
					＋ {type === 'const' ? `Turn ${value} into a knob` : 'New knob'}
				</button>
			{:else}
				<form class="flex gap-2 p-2" on:submit|preventDefault={applyNumber}>
					<input bind:this={numberInput} bind:value={draft} inputmode="decimal" aria-label="number value"
						class="rounded-md min-w-0 flex-1 border border-sc-line2 bg-sc-bg px-2 py-1 font-mono text-[12px] text-sc-ink outline-none focus:border-sc-ink" />
					<button type="submit" class="rounded-md border border-sc-line2 px-2 text-[12px] text-sc-ink hover:bg-sc-ink hover:text-black">Set</button>
				</form>
			{/if}
		</div>
	{/if}
</span>
