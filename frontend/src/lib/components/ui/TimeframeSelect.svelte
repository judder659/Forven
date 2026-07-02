<script lang="ts">
	import { ORDERED_TIMEFRAME_OPTIONS } from '$lib/config/timeframes';

	export let id = '';
	export let label = 'Timeframe';
	export let value = '1h';
	export let helpText = '';
	export let disabled = false;
</script>

<label class="block" for={id}>
	<div class="text-[10px] uppercase tracking-wider text-[#666]">{label}</div>
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
		<div class="mt-1 text-[11px] text-[#555]">{helpText}</div>
	{/if}
</label>
