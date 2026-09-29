<script lang="ts">
	import { ORDERED_TIMEFRAME_OPTIONS } from '$lib/config/timeframes';

	export let id = '';
	export let label = 'Timeframe';
	export let value = '1h';
	export let helpText = '';
	export let disabled = false;
</script>

<label class="block" for={id}>
	<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">{label}</div>
	<select
		id={id}
		bind:value
		{disabled}
		class="terminal-select mt-1.5"
	>
		{#each ORDERED_TIMEFRAME_OPTIONS as option}
			<option value={option.value}>{option.label}</option>
		{/each}
	</select>
	{#if helpText}
		<div class="mt-1 text-[11px] text-sc-ink3">{helpText}</div>
	{/if}
</label>
