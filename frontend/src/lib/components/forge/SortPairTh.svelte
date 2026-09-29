<script lang="ts">
	import { createEventDispatcher } from 'svelte';

	/**
	 * A header over a two-line metric cell (e.g. Sharpe with OOS Sharpe under it):
	 * each line sorts by its own field. aria-sort follows whichever one is active.
	 */
	export let primary: { field: string; label: string; title?: string };
	export let secondary: { field: string; label: string; title?: string };
	export let sortBy = '';
	export let direction: 'asc' | 'desc' = 'desc';
	export let thClass = '';
	export let align: 'left' | 'right' = 'right';

	const dispatch = createEventDispatcher<{ sort: string }>();

	$: primaryActive = sortBy === primary.field;
	$: secondaryActive = sortBy === secondary.field;
	$: ariaSort = (primaryActive || secondaryActive ? (direction === 'desc' ? 'descending' : 'ascending') : 'none') as
		| 'none'
		| 'ascending'
		| 'descending';
	$: glyph = direction === 'desc' ? '▼' : '▲';
</script>

<th class={`px-2 py-1.5 align-bottom font-normal ${align === 'left' ? 'text-left' : 'text-right'} ${thClass}`} aria-sort={ariaSort}>
	<div class={`flex flex-col leading-tight ${align === 'left' ? 'items-start' : 'items-end'}`}>
		<button
			type="button"
			class="text-[12px] transition-colors hover:text-sc-ink focus:outline-none focus-visible:text-sc-ink {primaryActive ? 'text-sc-ink' : 'text-sc-ink3'}"
			title={primary.title ?? ''}
			on:click={() => dispatch('sort', primary.field)}
		>{primary.label}{#if primaryActive}<span aria-hidden="true" class="ml-0.5 text-[9px]">{glyph}</span>{/if}</button>
		<button
			type="button"
			class="text-[10.5px] transition-colors hover:text-sc-ink focus:outline-none focus-visible:text-sc-ink {secondaryActive ? 'text-sc-ink' : 'text-sc-ink4'}"
			title={secondary.title ?? ''}
			on:click={() => dispatch('sort', secondary.field)}
		>{secondary.label}{#if secondaryActive}<span aria-hidden="true" class="ml-0.5 text-[9px]">{glyph}</span>{/if}</button>
	</div>
</th>
