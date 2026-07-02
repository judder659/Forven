<script lang="ts">
	export let id = '';
	export let label = 'Symbol';
	export let value = '';
	export let helpText = '';
	export let placeholder = 'BTC/USDT';
	export let suggestions: string[] = [];
	export let disabled = false;

	// Stable per-instance fallback so the label's `for` always resolves to an
	// input even when no `id` prop is supplied. Computed once, not reactively,
	// so the association does not change between renders.
	const fallbackId = `symbol-input-${Math.random().toString(36).slice(2, 9)}`;
	$: inputId = id || fallbackId;
	$: datalistId = `${inputId}-suggestions`;
	$: uniqueSuggestions = Array.from(new Set(suggestions.map((item) => String(item ?? '').trim()).filter(Boolean)));

	// Normalize the bound value so raw surrounding whitespace never propagates to
	// consumers (which feed `value` straight into API symbol params).
	function normalizeValue(event: Event): void {
		const next = (event.currentTarget as HTMLInputElement).value.trim();
		if (next !== value) value = next;
	}
</script>

<label class="block" for={inputId}>
	<div class="text-[10px] uppercase tracking-wider text-[#666]">{label}</div>
	<input
		id={inputId}
		list={uniqueSuggestions.length > 0 ? datalistId : undefined}
		bind:value
		on:change={normalizeValue}
		{placeholder}
		{disabled}
		class="terminal-input mt-1.5"
	/>
	{#if uniqueSuggestions.length > 0}
		<datalist id={datalistId}>
			{#each uniqueSuggestions as suggestion}
				<option value={suggestion}></option>
			{/each}
		</datalist>
	{/if}
	{#if helpText}
		<div class="mt-1 text-[11px] text-[#555]">{helpText}</div>
	{/if}
</label>
