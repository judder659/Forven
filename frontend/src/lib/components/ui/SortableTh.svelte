<script lang="ts">
	import { createEventDispatcher } from 'svelte';

	/** The sort field this header controls. Emitted via the `sort` event on activation. */
	export let field: string;
	export let label: string;
	export let title = '';
	/** True when the table is currently sorted by this field. */
	export let active = false;
	export let direction: 'asc' | 'desc' = 'desc';
	/** Extra classes for the <th> (e.g. column borders/right-align). */
	export let thClass = '';

	const dispatch = createEventDispatcher<{ sort: string }>();

	$: ariaSort = (active ? (direction === 'desc' ? 'descending' : 'ascending') : 'none') as
		| 'none'
		| 'ascending'
		| 'descending';
	$: indicator = active ? (direction === 'desc' ? ' ▼' : ' ▲') : '';
</script>

<!--
  Accessible sortable column header: the <th> carries aria-sort (so assistive tech
  announces sort state), and the clickable label is a real <button> so it is
  keyboard-focusable and Enter/Space-activatable. The glyph is aria-hidden because
  aria-sort already conveys direction.
-->
<th class={`py-2 px-2 text-left ${thClass}`} aria-sort={ariaSort} {title}>
	<button
		type="button"
		class="flex w-full items-center text-left text-[10px] uppercase tracking-wider text-[#666] transition-colors hover:text-white focus:outline-none focus-visible:text-white"
		on:click={() => dispatch('sort', field)}
	>
		<span>{label}</span>
		<span aria-hidden="true" class="whitespace-pre">{indicator}</span>
	</button>
</th>
