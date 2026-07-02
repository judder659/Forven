<script lang="ts">
	export let id = '';
	export let label = '';
	export let value: number | string = '';
	export let helpText = '';
	export let min: number | string | undefined = undefined;
	export let max: number | string | undefined = undefined;
	export let step: number | string | undefined = undefined;
	export let placeholder = '';
</script>

<label class="block" for={id}>
	<div class="text-[10px] uppercase tracking-wider text-[#666]">{label}</div>
	<input
		id={id}
		type="number"
		bind:value
		{min}
		{max}
		{step}
		{placeholder}
		class="terminal-input mt-1.5"
	/>
	{#if helpText}
		<div class="mt-1 text-[11px] text-[#555]">{helpText}</div>
	{/if}
</label>
