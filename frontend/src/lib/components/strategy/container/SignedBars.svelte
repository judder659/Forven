<script lang="ts">
	import { signClass } from '$lib/utils/strategyContainer/format';

	/** Horizontal bars from a zero line: gains right in teal, losses left in red-orange. */
	export let rows: Array<{ label: string; sub?: string; value: number | null; title?: string }> = [];
	export let format: (value: number) => string = (value) => value.toFixed(2);
	export let testid = '';

	$: values = rows.map((row) => row.value).filter((value): value is number => typeof value === 'number' && Number.isFinite(value));
	$: maxAbs = Math.max(1e-9, ...values.map((value) => Math.abs(value)));
	$: zero = values.some((value) => value < 0) ? 35 : 0;
</script>

<div class="grid gap-2" data-testid={testid || undefined}>
	{#each rows as row (row.label)}
		{@const value = typeof row.value === 'number' && Number.isFinite(row.value) ? row.value : null}
		<div class="grid grid-cols-[minmax(0,8em)_minmax(0,1fr)_auto] items-center gap-2.5" title={row.title ?? ''}>
			<span class="min-w-0 text-[12px] text-[#aab1bc]">
				{row.label}
				{#if row.sub}<small class="block text-[11px] text-[#666]">{row.sub}</small>{/if}
			</span>
			<span class="relative h-4">
				<span class="absolute -top-0.5 -bottom-0.5 w-px bg-[#2a2f38]" style={`left:${zero}%`}></span>
				{#if value !== null && value >= 0}
					<i class="absolute top-[3px] h-2.5 bg-[#139a9f]" style={`left:${zero}%;width:${(value / maxAbs) * (100 - zero)}%`}></i>
				{:else if value !== null}
					<i class="absolute top-[3px] h-2.5 bg-[#e0663f]" style={`left:${zero - (Math.abs(value) / maxAbs) * zero}%;width:${(Math.abs(value) / maxAbs) * zero}%`}></i>
				{/if}
			</span>
			<span class={`min-w-[5.5em] text-right text-[12px] tabular-nums ${signClass(value)}`}>{value === null ? '—' : format(value)}</span>
		</div>
	{/each}
</div>
